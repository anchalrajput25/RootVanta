"""Background worker: picks jobs from the queue and runs them.

    python -m incident_agent.workflow        # start one worker; start more for more capacity

Two kinds of job:

investigate  run the investigator on an incident, save the report, and let
             the agent account propose the fix it found. People approve it.
map          read recent traces of every service and update the dependency map.
execute      apply an approved plan in stages, check each stage with real
             metrics, and undo the change if a check FAILS.

Every job runs inside its own organization (row-level security), so one
company's job can never read or change another company's data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from incident_agent.api.audit import record
from incident_agent.db.models import (
    ExecutionRow,
    IncidentRow,
    InvestigationMetricRow,
    InvestigationRow,
    RecoveryPlanRow,
    VerificationRow,
)
from incident_agent.domain.models import ActionSpec, IncidentStatus, utcnow
from incident_agent.integrations.store import NotConnected
from incident_agent.investigation import report
from incident_agent.knowledge import memory, service_map
from incident_agent.investigation.collectors import Collectors
from incident_agent.investigation.graph import investigate
from incident_agent.investigation.rules import Runbook
from incident_agent.remediation.executor import Executor
from incident_agent.remediation.runner import Runner, Step, Timing
from incident_agent.telemetry.prometheus import PrometheusClient
from incident_agent.workflow import plans, queue
from incident_agent.workflow.notify import Notifier

log = logging.getLogger(__name__)

# Final execution status -> incident status
INCIDENT_AFTER = {
    "verified": IncidentStatus.RESOLVED,
    "partially_verified": IncidentStatus.VERIFYING,
    "unverified": IncidentStatus.VERIFYING,
    "rolled_back": IncidentStatus.OPEN,
    "apply_failed": IncidentStatus.OPEN,
    "undo_failed": IncidentStatus.OPEN,
    "interrupted": IncidentStatus.OPEN,
}
MESSAGES = {
    "verified": "Fixed and proven: {action} on {service}. The metrics are back to normal.",
    "partially_verified": "{action} on {service}: the main checks passed, an optional one did not. Please review.",
    "unverified": "{action} on {service} was applied, but there is not enough data to prove it worked. Please check.",
    "rolled_back": "{action} on {service} did NOT work, so it was undone automatically. A person needs to look.",
    "apply_failed": "{action} on {service} could not be applied; nothing was left changed. A person needs to look.",
    "undo_failed": "URGENT: {action} on {service} failed AND could not be undone. Restore it by hand.",
    "interrupted": "{action} on {service} was interrupted mid-way. Check the system's state by hand.",
}


@dataclass
class WorkerDeps:
    """Everything a worker talks to. Each takes the organization id, so each
    company can have its own monitoring and systems."""

    session_factory: sessionmaker[Session]
    collectors: Callable[[str], Collectors]
    prometheus: Callable[[str], PrometheusClient]
    executor: Callable[[str], Executor]
    runbook: Runbook = field(default_factory=Runbook)
    timing: Timing = field(default_factory=Timing)
    notifier: Notifier = field(default_factory=Notifier)
    use_llm: bool = False
    runner_factory: Optional[Callable[..., Runner]] = None  # tests swap the clock
    clock: Callable[[], datetime] = utcnow  # end of the investigation window


def org_session(deps: WorkerDeps, org_id: str) -> Session:
    db = deps.session_factory()
    db.info["org_id"] = org_id
    return db


def run_once(deps: WorkerDeps, worker_id: str) -> bool:
    """Claim and run one job. Returns False when the queue is empty."""
    job = queue.claim(deps.session_factory, worker_id)
    if job is None:
        return False
    handler = {"investigate": handle_investigate, "execute": handle_execute, "map": handle_map}.get(job.kind)
    try:
        if handler is None:
            raise ValueError(f"unknown job kind {job.kind}")
        if job.attempts > queue.MAX_ATTEMPTS:
            raise RuntimeError(f"gave up after {queue.MAX_ATTEMPTS} attempts")
        handler(deps, job)
    except Exception as e:  # a broken job must not stop the worker
        if isinstance(e, NotConnected):
            log.warning("job %s: %s", job.id, e)
        else:
            log.exception("job %s failed", job.id)
        with org_session(deps, job.org_id) as db:
            _mark_failed(deps, db, job, e)
            queue.finish(db, job.id, error=f"{type(e).__name__}: {e}"[:2000])
            db.commit()
    return True


def _mark_failed(deps: WorkerDeps, db: Session, job: queue.ClaimedJob, error: Exception) -> None:
    if job.kind == "investigate":
        inv = db.get(InvestigationRow, job.payload.get("investigation_id"))
        if inv is not None and inv.status != "done":
            inv.status = "failed"
            # Shown on the dashboard, so the company can fix it without reading server logs.
            inv.summary = str(error)[:1000] if isinstance(error, NotConnected) else \
                f"The investigation stopped with an error: {type(error).__name__}: {str(error)[:500]}"
    elif job.kind == "execute":
        exe = db.get(ExecutionRow, job.payload.get("execution_id"))
        if exe is not None and exe.finished_at is None:
            plan = db.get(RecoveryPlanRow, exe.plan_id)
            # It may have stopped half-way, so it is never retried automatically.
            _finish_execution(deps, db, job, exe, plan, "interrupted", [])


# --- investigate --------------------------------------------------------------------


def handle_investigate(deps: WorkerDeps, job: queue.ClaimedJob) -> None:
    with org_session(deps, job.org_id) as db:
        inv = db.get(InvestigationRow, job.payload["investigation_id"])
        incident = db.get(IncidentRow, inv.incident_id)
        inv.status = "running"
        if incident.status == IncidentStatus.OPEN.value:
            incident.status = IncidentStatus.INVESTIGATING.value
        db.commit()
        service, environment, minutes = incident.service, incident.environment, job.payload.get("minutes", 15)

    collectors = deps.collectors(job.org_id)
    if collectors.known_callees is None:
        collectors = replace(collectors, known_callees=service_map.callees_reader(deps.session_factory, job.org_id))
    result = investigate(collectors, service, minutes=minutes, environment=environment,
                         runbook=deps.runbook, use_llm=deps.use_llm, end=deps.clock())

    with org_session(deps, job.org_id) as db:
        inv = db.get(InvestigationRow, job.payload["investigation_id"])
        incident = db.get(IncidentRow, inv.incident_id)
        service_map.save(db, job.org_id, result.service_calls)
        if result.conclusion == "cause_identified":
            top = result.hypotheses[0]
            past = memory.similar(db, job.org_id, incident.service, top.cause.value, top.service,
                                  exclude_incident=incident.id)
            seen_before = memory.notes(result, past)
            if seen_before:
                result = result.model_copy(update={"notes": (*result.notes, *seen_before)})
        else:
            seen_before = []
        inv.status, inv.conclusion, inv.summary = "done", result.conclusion, result.summary
        inv.report, inv.report_markdown = report.to_json(result), report.to_markdown(result)
        ai = result.ai_usage
        db.add(InvestigationMetricRow(
            org_id=job.org_id, investigation_id=inv.id, conclusion=result.conclusion, engine=result.engine,
            duration_ms=int(result.duration_seconds * 1000), ai_model=ai.model if ai else None,
            input_tokens=ai.input_tokens if ai else 0, output_tokens=ai.output_tokens if ai else 0,
            cost_usd=ai.cost_usd if ai else 0.0))
        record(db, job.org_id, None, f"investigation.{result.conclusion}", "investigation", inv.id,
               {"summary": result.summary[:300], "engine": result.engine})
        message = f"Investigation of {service}: {result.summary}"
        if result.proposal is not None:
            agent = plans.agent_user(db, job.org_id, incident.team_id)
            plan = plans.create_plan(db, incident, result.proposal.action, result.proposal.rationale, agent)
            inv.plan_id = plan.id
            if plan.status == plans.AWAITING_APPROVAL:
                message += f" Proposed fix waiting for approval: {result.proposal.rationale}"
            elif plan.status == plans.BLOCKED:
                message += " The proposed fix was blocked by the safety policy."
        memory.remember(db, job.org_id, incident, inv.id, result, inv.plan_id)
        if seen_before:
            message += " " + " ".join(seen_before)
        if result.conclusion == "no_anomaly" and incident.status == IncidentStatus.INVESTIGATING.value:
            incident.status = IncidentStatus.OPEN.value
        queue.finish(db, job.id)
        deps.notifier.send(db, job.org_id, message, incident_id=incident.id)
        db.commit()


# --- dependency map -----------------------------------------------------------------


def handle_map(deps: WorkerDeps, job: queue.ClaimedJob) -> None:
    jaeger = deps.collectors(job.org_id).jaeger
    if jaeger is None:
        raise NotConnected("Jaeger is not connected, so the dependency map cannot be learned. "
                           "An admin can connect it in Settings > Monitoring.")
    found = service_map.scan(jaeger, end=deps.clock())
    with org_session(deps, job.org_id) as db:
        new = service_map.save(db, job.org_id, found.calls)
        record(db, job.org_id, job.payload.get("requested_by"), "service_map.refreshed", "service_map", job.id,
               {"services": len(found.services), "edges": len(found.calls), "new_edges": new,
                "unreadable": found.failed[:20]})
        queue.finish(db, job.id)
        db.commit()


# --- execute ------------------------------------------------------------------------


def handle_execute(deps: WorkerDeps, job: queue.ClaimedJob) -> None:
    execution_id = job.payload["execution_id"]
    with org_session(deps, job.org_id) as db:
        exe = db.get(ExecutionRow, execution_id)
        plan = db.scalar(select(RecoveryPlanRow).where(RecoveryPlanRow.id == exe.plan_id).with_for_update())
        if plan.status != plans.QUEUED:
            # Claimed again after a worker died mid-run. Never apply a fix twice:
            # the system may be half-changed, so a person has to look.
            _finish_execution(deps, db, job, exe, plan, "interrupted", [])
            return
        plan.status = exe.status = plans.EXECUTING
        incident = db.get(IncidentRow, plan.incident_id)
        incident.status = IncidentStatus.MITIGATING.value
        record(db, job.org_id, exe.requested_by, "execution.started", "recovery_plan", plan.id, {"execution_id": exe.id})
        db.commit()
        action = ActionSpec.model_validate(plan.action)
        watch_service = incident.service

    def save_step(step: Step) -> None:
        with org_session(deps, job.org_id) as s:
            row = s.get(ExecutionRow, execution_id)
            row.steps = [*row.steps, step.to_dict()]
            if step.kind == "preview":
                row.preview = step.data
            s.commit()

    make = deps.runner_factory or Runner
    runner = make(executor=deps.executor(job.org_id), prometheus=deps.prometheus(job.org_id), timing=deps.timing,
                  on_step=save_step)
    result = runner.run(action, watch_service, limits=_limits(deps.runbook, watch_service))

    with org_session(deps, job.org_id) as db:
        exe = db.get(ExecutionRow, execution_id)
        plan = db.get(RecoveryPlanRow, exe.plan_id)
        _finish_execution(deps, db, job, exe, plan, result.status, result.verdicts)


def _limits(runbook: Runbook, service: str) -> dict[str, float]:
    return dict(runbook.services.get(service, {}).get("verify") or {})


def _finish_execution(deps: WorkerDeps, db: Session, job: queue.ClaimedJob, exe: ExecutionRow,
                      plan: RecoveryPlanRow, status: str, verdicts: list) -> None:
    exe.status, exe.finished_at = status, utcnow()
    plan.status = status
    for stage, verdict in verdicts:
        stored = verdict.model_dump(mode="json")
        stored.update(should_roll_back=verdict.should_roll_back, stage=stage, execution_id=exe.id)
        db.add(VerificationRow(org_id=job.org_id, plan_id=plan.id, status=verdict.status.value, verdict=stored,
                               requested_by=exe.requested_by))
    incident = db.get(IncidentRow, plan.incident_id)
    incident.status = INCIDENT_AFTER[status].value
    memory.record_outcome(db, job.org_id, plan.id, status)
    record(db, job.org_id, exe.requested_by, f"execution.{status}", "recovery_plan", plan.id,
           {"execution_id": exe.id, "stages": [s for s, _ in verdicts]})
    queue.finish(db, job.id)
    action = plan.action
    deps.notifier.send(db, job.org_id, MESSAGES[status].format(action=action["type"], service=action["service"]),
                       incident_id=incident.id)
    db.commit()
