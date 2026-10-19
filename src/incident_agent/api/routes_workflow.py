"""Step 5 endpoints: investigations, practice runs, running approved fixes,
alerts from monitoring, and notification channels."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from incident_agent.api.audit import record
from incident_agent.integrations.store import NotConnected, check_url
from incident_agent.api.deps import Caller, get_anonymous_db, get_caller
from incident_agent.api.schemas import (
    AlertsReceived,
    AlertSourceCreated,
    AlertSourceIn,
    AlertSourceOut,
    ExecutionOut,
    InvestigationIn,
    InvestigationOut,
    JobOut,
    NotificationTargetIn,
    NotificationTargetOut,
    StatsOut,
)
from incident_agent.db.models import (
    AlertSourceRow,
    ExecutionRow,
    IncidentAlertRow,
    IncidentRow,
    InvestigationMetricRow,
    InvestigationRow,
    JobRow,
    NotificationTargetRow,
    RecoveryPlanRow,
    TeamRow,
)
from incident_agent.domain.models import ActionSpec, IncidentStatus, Role, Severity, new_id, utcnow
from incident_agent.knowledge import service_map
from incident_agent.remediation.executor import ExecutorError
from incident_agent.workflow import plans, queue

router = APIRouter(tags=["workflow"])

OPEN_STATES = {s.value for s in IncidentStatus} - {IncidentStatus.RESOLVED.value, IncidentStatus.CLOSED.value}


def _enqueue_investigation(db: Session, incident: IncidentRow, minutes: int,
                           requested_by: Optional[str]) -> InvestigationRow:
    inv_id = new_id("inv")
    job = queue.enqueue(db, incident.org_id, "investigate", {"investigation_id": inv_id, "minutes": minutes})
    inv = InvestigationRow(id=inv_id, org_id=incident.org_id, incident_id=incident.id, job_id=job.id,
                           status="queued", requested_by=requested_by)
    db.add(inv)
    record(db, incident.org_id, requested_by, "investigation.queued", "incident", incident.id,
           {"investigation_id": inv_id, "minutes": minutes})
    return inv


# --- investigations ---------------------------------------------------------------


@router.post("/incidents/{incident_id}/investigations", response_model=InvestigationOut,
             status_code=status.HTTP_202_ACCEPTED)
def start_investigation(incident_id: str, body: InvestigationIn = Body(default_factory=InvestigationIn),
                        caller: Caller = Depends(get_caller)) -> InvestigationRow:
    """Queue an investigation. A worker runs it and, if it finds a cause, the
    agent proposes a fix that people then approve."""
    caller.require(Role.RESPONDER, Role.APPROVER)
    incident = caller.get(IncidentRow, incident_id)
    inv = _enqueue_investigation(caller.db, incident, body.minutes, caller.user.id)
    caller.db.commit()
    return inv


@router.get("/incidents/{incident_id}/investigations", response_model=list[InvestigationOut])
def list_investigations(incident_id: str, caller: Caller = Depends(get_caller)) -> list[InvestigationRow]:
    incident = caller.get(IncidentRow, incident_id)
    stmt = select(InvestigationRow).where(InvestigationRow.incident_id == incident.id,
                                          InvestigationRow.org_id == caller.org_id)
    return list(caller.db.scalars(stmt.order_by(InvestigationRow.created_at.desc())))


@router.get("/investigations/{investigation_id}", response_model=InvestigationOut)
def get_investigation(investigation_id: str, caller: Caller = Depends(get_caller)) -> InvestigationRow:
    return caller.get(InvestigationRow, investigation_id)


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, caller: Caller = Depends(get_caller)) -> JobRow:
    return caller.get(JobRow, job_id)


# --- practice run and execution -------------------------------------------------------


@router.post("/plans/{plan_id}/dry-run")
def dry_run(plan_id: str, request: Request, caller: Caller = Depends(get_caller)) -> dict[str, Any]:
    """Show exactly what the fix would change, and how it would be undone. Changes nothing."""
    caller.require(Role.RESPONDER, Role.APPROVER)
    plan = caller.get(RecoveryPlanRow, plan_id)
    if plan.status in (plans.BLOCKED, plans.REJECTED):
        raise HTTPException(status.HTTP_409_CONFLICT, f"plan is {plan.status}")
    try:
        executor = request.app.state.connections.executor(caller.org_id)
    except NotConnected as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
    try:
        preview = executor.preview(ActionSpec.model_validate(plan.action))
    except ExecutorError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from None
    record(caller.db, caller.org_id, caller.user.id, "plan.dry_run", "recovery_plan", plan.id,
           {"changes": preview.changes})
    caller.db.commit()
    return preview.to_dict()


@router.post("/plans/{plan_id}/execute", response_model=ExecutionOut, status_code=status.HTTP_202_ACCEPTED)
def execute_plan(plan_id: str, request: Request, caller: Caller = Depends(get_caller)) -> ExecutionRow:
    """Queue an approved plan. A worker applies it in stages, checks each stage
    with real metrics, and undoes it automatically if a check FAILS."""
    caller.require(Role.RESPONDER, Role.APPROVER)
    plan = caller.db.scalar(
        select(RecoveryPlanRow).where(RecoveryPlanRow.id == plan_id, RecoveryPlanRow.org_id == caller.org_id)
        .with_for_update()
    )
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "recovery_plan not found")
    if plan.status != plans.READY:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"plan is {plan.status}; only an allowed and approved plan can run")
    try:  # the fix needs somewhere to apply it and metrics to prove it; say so now, not half-way
        request.app.state.connections.executor(caller.org_id)
        request.app.state.connections.prometheus(caller.org_id)
    except NotConnected as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
    exe_id = new_id("exe")
    job = queue.enqueue(caller.db, caller.org_id, "execute", {"execution_id": exe_id})
    exe = ExecutionRow(id=exe_id, org_id=caller.org_id, plan_id=plan.id, job_id=job.id, status=plans.QUEUED,
                       steps=[], requested_by=caller.user.id)
    caller.db.add(exe)
    try:
        caller.db.flush()
    except IntegrityError:
        caller.db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "this plan has already been run") from None
    plan.status = plans.QUEUED
    record(caller.db, caller.org_id, caller.user.id, "execution.queued", "recovery_plan", plan.id,
           {"execution_id": exe.id})
    caller.db.commit()
    return exe


@router.get("/plans/{plan_id}/execution", response_model=ExecutionOut)
def get_execution(plan_id: str, caller: Caller = Depends(get_caller)) -> ExecutionRow:
    plan = caller.get(RecoveryPlanRow, plan_id)
    exe = caller.db.scalar(select(ExecutionRow).where(ExecutionRow.plan_id == plan.id,
                                                      ExecutionRow.org_id == caller.org_id))
    if exe is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "this plan has not been run")
    return exe


@router.get("/stats", response_model=StatsOut)
def stats(caller: Caller = Depends(get_caller)) -> StatsOut:
    """Counts for the dashboard: open work, and how many fixes were proven or undone."""

    def counts(model) -> dict[str, int]:
        rows = caller.db.execute(select(model.status, func.count()).where(model.org_id == caller.org_id)
                                 .group_by(model.status))
        return {status_: n for status_, n in rows}

    m = InvestigationMetricRow
    mine = m.org_id == caller.org_id
    recent = sorted(caller.db.scalars(select(m.duration_ms).where(mine).order_by(m.created_at.desc()).limit(500)))
    month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    cost, ai_runs = caller.db.execute(
        select(func.coalesce(func.sum(m.cost_usd), 0.0), func.count(m.ai_model))
        .where(mine, m.created_at >= month_start)).one()
    return StatsOut(
        incidents_by_status=counts(IncidentRow), fixes_by_result=counts(ExecutionRow),
        investigations_done=caller.db.scalar(select(func.count()).select_from(m).where(mine)) or 0,
        median_investigation_seconds=recent[len(recent) // 2] / 1000 if recent else None,
        p95_investigation_seconds=recent[int(0.95 * (len(recent) - 1))] / 1000 if recent else None,
        ai_cost_usd_this_month=round(float(cost), 4), ai_investigations_this_month=ai_runs)


# --- alerts from monitoring -------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@router.post("/alert-sources", response_model=AlertSourceCreated, status_code=status.HTTP_201_CREATED)
def create_alert_source(body: AlertSourceIn, caller: Caller = Depends(get_caller)) -> dict[str, Any]:
    caller.require(Role.ADMIN)
    caller.get(TeamRow, body.team_id)
    token = "ias_" + secrets.token_urlsafe(32)
    row = AlertSourceRow(org_id=caller.org_id, team_id=body.team_id, name=body.name,
                         environment=body.environment.value, token_hash=_token_hash(token), created_by=caller.user.id)
    caller.db.add(row)
    caller.db.flush()
    record(caller.db, caller.org_id, caller.user.id, "alert_source.created", "alert_source", row.id,
           {"name": row.name})
    caller.db.commit()
    return {**AlertSourceOut.model_validate(row).model_dump(), "token": token}


@router.get("/alert-sources", response_model=list[AlertSourceOut])
def list_alert_sources(caller: Caller = Depends(get_caller)) -> list[AlertSourceRow]:
    return list(caller.db.scalars(select(AlertSourceRow).where(AlertSourceRow.org_id == caller.org_id)))


@router.delete("/alert-sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_alert_source(source_id: str, caller: Caller = Depends(get_caller)) -> None:
    """Revoke a token, e.g. after it leaked. Alerts sent with it are refused from now on."""
    caller.require(Role.ADMIN)
    row = caller.get(AlertSourceRow, source_id)
    caller.db.delete(row)
    record(caller.db, caller.org_id, caller.user.id, "alert_source.deleted", "alert_source", source_id,
           {"name": row.name})
    caller.db.commit()


SEVERITIES = {s.value for s in Severity}
SEVERITY_WORDS = {"critical": Severity.SEV1, "page": Severity.SEV1, "error": Severity.SEV2,
                  "warning": Severity.SEV3, "info": Severity.SEV4}


def _severity(label: Optional[str]) -> Severity:
    label = (label or "").lower()
    return Severity(label) if label in SEVERITIES else SEVERITY_WORDS.get(label, Severity.SEV3)


# An alert for a service that calls, or is called by, a service already in an
# incident opened this recently joins that incident: one outage, one incident.
# "In an incident" means any service that alerted into it, so a failure that
# spreads up a chain of callers stays one incident.
GROUP_WINDOW = timedelta(minutes=30)


def _group_into(db: Session, org_id: str, team_id: str, service: str) -> tuple[Optional[IncidentRow], str]:
    """The open incident this alert belongs to, and why; or (None, "")."""
    open_ = (IncidentRow.org_id == org_id) & (IncidentRow.team_id == team_id) & IncidentRow.status.in_(OPEN_STATES)
    same = db.scalar(select(IncidentRow).where(open_, IncidentRow.service == service)
                     .order_by(IncidentRow.created_at.desc()))
    if same is not None:
        return same, "same service"
    near = service_map.neighbours(db, org_id, service)
    related = near.callees | near.callers
    if not related:
        return None, ""
    hits = db.execute(
        select(IncidentRow, IncidentAlertRow.service)
        .join(IncidentAlertRow, IncidentAlertRow.incident_id == IncidentRow.id)
        .where(open_, IncidentAlertRow.service.in_(related), IncidentRow.created_at >= utcnow() - GROUP_WINDOW)
        .order_by(IncidentRow.created_at, IncidentAlertRow.received_at)).all()
    if not hits:
        return None, ""
    # A service this one calls is the likelier cause, so prefer the incident it is in.
    incident, other = next(((i, o) for i, o in hits if o in near.callees), hits[0])
    if other in near.callees:
        return incident, f"{service} calls {other}"
    return incident, f"{other} calls {service}"


@router.post("/alerts/alertmanager", response_model=AlertsReceived)
def receive_alertmanager(payload: dict[str, Any] = Body(...), authorization: Optional[str] = Header(None),
                         db: Session = Depends(get_anonymous_db)) -> AlertsReceived:
    """Webhook for Prometheus Alertmanager. Each firing alert with a ``service``
    label opens an incident and starts an investigation, unless it belongs to an
    open one: the same service, or a service next to it in the dependency map
    whose incident opened in the last 30 minutes. Then it is grouped into it."""
    token = (authorization or "").removeprefix("Bearer ").strip()
    db.info["auth_lookup"] = True  # find the source by token before its organization is known
    source = db.execute(select(AlertSourceRow.id, AlertSourceRow.org_id, AlertSourceRow.team_id,
                               AlertSourceRow.environment, AlertSourceRow.name)
                        .where(AlertSourceRow.token_hash == _token_hash(token))).first() if token else None
    db.rollback()
    db.info["auth_lookup"] = False
    if source is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown alert source token")
    source_id, org_id, team_id, environment, source_name = source
    db.info["org_id"] = org_id

    opened, grouped, ignored = [], [], 0
    agent = None
    for alert in payload.get("alerts") or []:
        labels = alert.get("labels") or {}
        service = str(labels.get("service") or "")[:200]
        if alert.get("status", "firing") != "firing" or not service:
            ignored += 1
            continue
        alertname = str(labels.get("alertname", "Alert"))[:200]
        severity = _severity(labels.get("severity"))
        existing, reason = _group_into(db, org_id, team_id, service)
        if existing is not None:
            grouped.append(existing.id)
            db.add(IncidentAlertRow(org_id=org_id, incident_id=existing.id, service=service, alertname=alertname,
                                    severity=severity.value, source=source_name[:200], reason=reason))
            if severity.value < existing.severity:  # "sev1" < "sev3": a worse alert raises the incident
                record(db, org_id, None, "incident.severity_raised", "incident", existing.id,
                       {"from": existing.severity, "to": severity.value, "alertname": alertname})
                existing.severity = severity.value
            record(db, org_id, None, "alert.grouped", "incident", existing.id,
                   {"alertname": alertname, "service": service, "reason": reason, "source": source_name})
            continue
        agent = agent or plans.agent_user(db, org_id, team_id)
        summary = str((alert.get("annotations") or {}).get("summary") or "")[:200]
        title = f"{alertname}: {summary or service}"[:300]
        started = _parse_time(alert.get("startsAt")) or utcnow()
        incident = IncidentRow(org_id=org_id, team_id=team_id, title=title, service=service,
                               environment=environment, severity=severity.value,
                               status=IncidentStatus.OPEN.value, started_at=started, created_by=agent.id)
        db.add(incident)
        db.flush()
        db.add(IncidentAlertRow(org_id=org_id, incident_id=incident.id, service=service, alertname=alertname,
                                severity=severity.value, source=source_name[:200], reason="opened the incident"))
        record(db, org_id, None, "incident.created", "incident", incident.id,
               {"title": title, "service": service, "source": source_name})
        _enqueue_investigation(db, incident, 15, None)
        opened.append(incident.id)
    db.commit()
    return AlertsReceived(incidents_opened=opened, grouped_into_existing=grouped, ignored=ignored)


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.year > 2000 and t.tzinfo else None  # Alertmanager sends year 1 for "unset"


# --- notification channels ----------------------------------------------------------------


def _hint(url: str) -> str:
    return url if len(url) <= 32 else f"{url[:24]}…{url[-4:]}"


@router.post("/notification-targets", response_model=NotificationTargetOut, status_code=status.HTTP_201_CREATED)
def add_notification_target(body: NotificationTargetIn, caller: Caller = Depends(get_caller)) -> NotificationTargetOut:
    caller.require(Role.ADMIN)
    try:
        check_url(body.url)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    row = NotificationTargetRow(org_id=caller.org_id, url=body.url, created_by=caller.user.id)
    caller.db.add(row)
    caller.db.flush()
    record(caller.db, caller.org_id, caller.user.id, "notification_target.created", "notification_target", row.id)
    caller.db.commit()
    return NotificationTargetOut(id=row.id, url_hint=_hint(row.url), created_at=row.created_at)


@router.get("/notification-targets", response_model=list[NotificationTargetOut])
def list_notification_targets(caller: Caller = Depends(get_caller)) -> list[NotificationTargetOut]:
    caller.require(Role.ADMIN)
    rows = caller.db.scalars(select(NotificationTargetRow).where(NotificationTargetRow.org_id == caller.org_id))
    return [NotificationTargetOut(id=r.id, url_hint=_hint(r.url), created_at=r.created_at) for r in rows]


@router.post("/notification-targets/{target_id}/test")
def test_notification_target(target_id: str, caller: Caller = Depends(get_caller)) -> dict[str, Any]:
    caller.require(Role.ADMIN)
    row = caller.get(NotificationTargetRow, target_id)
    try:
        httpx.post(row.url, json={"text": "Test message from Rootvanta: notifications work."},
                   timeout=5.0).raise_for_status()
        return {"ok": True, "message": "Sent. Check the channel for the test message."}
    except httpx.HTTPStatusError as e:
        return {"ok": False, "message": f"The channel refused it (HTTP {e.response.status_code}). Check the address."}
    except httpx.HTTPError:
        return {"ok": False, "message": "Could not reach the channel. Check the address."}


@router.delete("/notification-targets/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_notification_target(target_id: str, caller: Caller = Depends(get_caller)) -> None:
    caller.require(Role.ADMIN)
    row = caller.get(NotificationTargetRow, target_id)
    caller.db.delete(row)
    record(caller.db, caller.org_id, caller.user.id, "notification_target.deleted", "notification_target", target_id)
    caller.db.commit()
