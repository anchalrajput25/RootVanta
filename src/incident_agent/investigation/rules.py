"""Deterministic reasoning: evidence -> timeline, ranked causes, and a fix from the allowed list.

This engine needs no AI model, so it always works and is fully testable. The
optional model step (llm.py) can re-rank and explain, but the fix proposal is
always produced here, from code, never from model text.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml

from incident_agent.investigation.collectors import CHANGE_LOOKBACK
from incident_agent.domain.models import ActionSpec, ActionType, EvidenceKind, RiskLevel
from incident_agent.investigation.models import (
    CauseKind,
    Evidence,
    Hypothesis,
    Proposal,
    TimelineEvent,
)

# Below this, we report "insufficient evidence" instead of naming a cause.
CONFIDENCE_THRESHOLD = 0.6
CLOCK_SLACK = timedelta(seconds=60)  # metrics are 1-minute rates, so onset lags the real start
TARGET_CPU = 0.6
# Errors a deploy introduces show up within this time after it.
NEW_ERROR_WINDOW = timedelta(minutes=3)


class Runbook:
    """Per-service knowledge a team writes down once: safe fallbacks and limits."""

    def __init__(self, data: Optional[dict[str, Any]] = None):
        self.services: dict[str, Any] = (data or {}).get("services", {}) or {}

    @classmethod
    def load(cls, path: Optional[Path]) -> "Runbook":
        if path is None or not path.exists():
            return cls()
        with path.open(encoding="utf-8") as f:
            return cls(yaml.safe_load(f))

    def fallback(self, service: str, dependency: str) -> Optional[dict[str, Any]]:
        return (self.services.get(service, {}).get("fallbacks") or {}).get(dependency)

    def max_replicas(self, service: str) -> int:
        return int(self.services.get(service, {}).get("max_replicas", 20))


# --- helpers over the evidence list --------------------------------------------------


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def anomalies(evidence: list[Evidence], service: str) -> list[Evidence]:
    return [e for e in evidence if e.kind == EvidenceKind.METRIC and e.service == service and e.facts.get("anomalous")]


def _metric(evidence: list[Evidence], service: str, signal: str) -> Optional[Evidence]:
    return next((e for e in evidence if e.kind == EvidenceKind.METRIC and e.service == service
                 and e.facts.get("signal") == signal), None)


def _logs(evidence: list[Evidence], service: str) -> list[Evidence]:
    return [e for e in evidence if e.kind == EvidenceKind.LOG and e.service == service]


def _trace_origin(evidence: list[Evidence], service: str) -> Optional[Evidence]:
    return next((e for e in evidence if e.kind == EvidenceKind.TRACE and e.service == service
                 and "origin_counts" in e.facts), None)


def onset(evidence: list[Evidence], service: str) -> Optional[datetime]:
    times = [_at(e.facts["onset"]) for e in anomalies(evidence, service) if "onset" in e.facts]
    return min(times) if times else None


# --- timeline -----------------------------------------------------------------------


def build_timeline(evidence: list[Evidence], service: str) -> list[TimelineEvent]:
    events = []
    for e in evidence:
        if e.kind in (EvidenceKind.DEPLOYMENT, EvidenceKind.INFRASTRUCTURE):
            events.append(TimelineEvent(at=e.observed_at, description=e.summary, evidence_id=e.id))
        elif e.kind == EvidenceKind.METRIC and e.facts.get("anomalous") and "onset" in e.facts:
            events.append(TimelineEvent(at=_at(e.facts["onset"]), description=e.summary, evidence_id=e.id))
        elif e.kind == EvidenceKind.LOG:
            events.append(TimelineEvent(at=_at(e.facts["first_seen"]),
                                        description=f"First '{e.facts['signature']}' error in {e.service}",
                                        evidence_id=e.id))
    return sorted(events, key=lambda t: t.at)


# --- hypotheses ---------------------------------------------------------------------


def _bad_deploys(evidence: list[Evidence], service: str, start: datetime) -> list[Hypothesis]:
    symptoms = [e.id for e in anomalies(evidence, service)]
    trace = _trace_origin(evidence, service)
    out = []
    deploys = [e for e in evidence if e.kind == EvidenceKind.DEPLOYMENT
               and start - CHANGE_LOOKBACK <= e.observed_at <= start + CLOCK_SLACK]
    latest: dict[str, Evidence] = {}
    for d in sorted(deploys, key=lambda e: e.observed_at):
        latest[d.service] = d  # the most recent deploy per service before the symptom
    for svc, d in latest.items():
        version = d.facts.get("to_version")
        new_errors = [e for e in _logs(evidence, svc)
                      if d.observed_at - timedelta(seconds=5) <= _at(e.facts["first_seen"]) <= d.observed_at + NEW_ERROR_WINDOW
                      and (not e.facts.get("version") or e.facts["version"] == version)]
        support, against = [*symptoms, d.id, *(e.id for e in new_errors)], []
        # Problems in the deployed service itself that began soon after the
        # deploy. Weaker than new error messages, but it is the only sign of
        # a deploy that made a service slow without any errors.
        own_start = start if svc == service else onset(evidence, svc)
        began_after = own_start is not None and \
            d.observed_at - CLOCK_SLACK <= own_start <= d.observed_at + NEW_ERROR_WINDOW + CLOCK_SLACK
        confidence = 0.45 + (0.3 if new_errors else 0.2 if began_after else 0.0)
        if trace and trace.facts.get("error_traces"):
            if trace.facts.get("top_origin") == svc:
                confidence += 0.1
                support.append(trace.id)
            else:
                confidence -= 0.2
                against.append(trace.id)
        detail = (f": new '{new_errors[0].facts['signature']}' errors started right after it" if new_errors
                  else f": {svc} got worse within {NEW_ERROR_WINDOW.seconds // 60} minutes of it" if began_after else "")
        out.append(Hypothesis(
            cause=CauseKind.BAD_DEPLOY, service=svc, confidence=round(min(confidence, 0.95), 2),
            statement=f"The deploy of {version} to {svc} at {d.observed_at:%H:%M:%S} broke it{detail}.",
            supporting_evidence=tuple(support), contradicting_evidence=tuple(against),
        ))
    return out


def _dependency_failures(evidence: list[Evidence], service: str) -> list[Hypothesis]:
    trace = _trace_origin(evidence, service)
    if not trace or not trace.facts.get("error_traces"):
        return []
    top = trace.facts.get("top_origin")
    share = trace.facts["origin_counts"].get(top, 0) / trace.facts["error_traces"]
    if top in (None, service) or share < 0.5:
        return []
    dep_anomalies = anomalies(evidence, top)
    dep_logs = _logs(evidence, top)
    confidence = 0.45 + 0.25 * share + (0.15 if dep_anomalies else 0.0) + (0.1 if dep_logs else 0.0)
    against = [e.id for e in evidence if e.kind == EvidenceKind.DEPLOYMENT and e.service == service]
    if against:
        confidence -= 0.1
    # A dependency that was itself just deployed is most likely a bad deploy
    # of that dependency (handled by _bad_deploys), not an outage to route around.
    start = onset(evidence, service)
    dep_deploys = [e.id for e in evidence if e.kind == EvidenceKind.DEPLOYMENT and e.service == top
                   and start is not None and start - CHANGE_LOOKBACK <= e.observed_at <= start + CLOCK_SLACK]
    if dep_deploys:
        confidence -= 0.3
        against += dep_deploys
    detail = f" ('{dep_logs[0].facts['signature']}')" if dep_logs else ""
    return [Hypothesis(
        cause=CauseKind.DEPENDENCY_FAILURE, service=top, confidence=round(min(confidence, 0.95), 2),
        statement=f"{top} is failing{detail}, and {service} fails when it calls {top}: "
                  f"{share:.0%} of failing {service} requests start there.",
        supporting_evidence=(*(e.id for e in anomalies(evidence, service)), trace.id,
                             *(e.id for e in dep_anomalies), *(e.id for e in dep_logs)),
        contradicting_evidence=tuple(against),
    )]


def _saturation(evidence: list[Evidence], service: str, start: datetime) -> list[Hypothesis]:
    cpu = _metric(evidence, service, "cpu_utilization")
    latency = _metric(evidence, service, "p95_latency_ms")
    if not cpu or not latency or not latency.facts.get("anomalous"):
        return []
    busy = cpu.facts.get("anomalous") or cpu.facts.get("latest", 0) >= 0.85
    if not busy:
        return []
    confidence = 0.8
    against = [e.id for e in evidence if e.kind == EvidenceKind.DEPLOYMENT and e.service == service
               and start - CHANGE_LOOKBACK <= e.observed_at <= start + CLOCK_SLACK]
    if against:
        confidence -= 0.15
    replicas = cpu.facts.get("replicas")
    on = f" on {replicas:g} replicas" if replicas else ""
    return [Hypothesis(
        cause=CauseKind.SATURATION, service=service, confidence=round(confidence, 2),
        statement=f"{service} is out of capacity: CPU at {cpu.facts['latest']:.0%}{on}, "
                  f"so requests queue and p95 latency rose to {latency.facts.get('after', 0):.0f} ms.",
        supporting_evidence=(latency.id, cpu.id), contradicting_evidence=tuple(against),
    )]


def rank_hypotheses(evidence: list[Evidence], service: str) -> list[Hypothesis]:
    start = onset(evidence, service)
    if start is None:
        return []
    found = [*_bad_deploys(evidence, service, start), *_dependency_failures(evidence, service),
             *_saturation(evidence, service, start)]
    return sorted(found, key=lambda h: h.confidence, reverse=True)


# --- fixes from the allowed list ------------------------------------------------------


def propose_fix(
    hypotheses: list[Hypothesis], evidence: list[Evidence], service: str, environment: str, runbook: Runbook
) -> tuple[Optional[Proposal], Optional[str]]:
    """Return (proposal, note). The action always comes from this code, never from model text."""
    if not hypotheses:
        return None, None
    top = hypotheses[0]
    by_id = {e.id: e for e in evidence}
    if top.cause == CauseKind.BAD_DEPLOY:
        deploy = next((by_id[i] for i in top.supporting_evidence
                       if i in by_id and by_id[i].kind == EvidenceKind.DEPLOYMENT), None)
        previous = deploy.facts.get("from_version") if deploy else None
        if not previous:
            return None, "The previous version is unknown, so no rollback target can be proposed."
        action = ActionSpec(type=ActionType.ROLLBACK_DEPLOYMENT, service=top.service, environment=environment,
                            params={"target_version": previous}, risk=RiskLevel.MEDIUM, reversible=True,
                            blast_radius_pct=25)
        why = f"Roll {top.service} back to {previous}, the version that ran before the errors began."
    elif top.cause == CauseKind.DEPENDENCY_FAILURE:
        fallback = runbook.fallback(service, top.service)
        if not fallback:
            return None, (f"The runbook lists no safe fallback for {service} when {top.service} fails; "
                          f"contact the owners of {top.service}.")
        action = ActionSpec(type=ActionType.TOGGLE_FEATURE_FLAG, service=service, environment=environment,
                            params={"flag": fallback["flag"], "value": True, "rollout_pct": 10},
                            risk=RiskLevel.MEDIUM, reversible=True, blast_radius_pct=10)
        why = f"{fallback.get('description', 'Use the fallback')} (runbook), starting with 10% of traffic."
    else:
        cpu = _metric(evidence, top.service, "cpu_utilization")
        current = int((cpu.facts.get("replicas") if cpu else 0) or 0)
        if current <= 0:
            return None, "The current replica count is unknown, so no scaling target can be proposed."
        load = max(cpu.facts.get("after", cpu.facts["latest"]), cpu.facts["latest"]) if cpu else 1.0
        # Assume real demand is at least what saturated CPU shows; aim for 60% CPU.
        wanted = max(current + 1, math.ceil(current * max(load, 1.0) / TARGET_CPU))
        replicas = min(wanted, runbook.max_replicas(top.service))
        action = ActionSpec(type=ActionType.SCALE_SERVICE, service=top.service, environment=environment,
                            params={"replicas": replicas}, risk=RiskLevel.LOW, reversible=True,
                            blast_radius_pct=0)
        why = f"Scale {top.service} from {current} to {replicas} replicas to bring CPU near {TARGET_CPU:.0%}."
    return Proposal(action=action, rationale=why, hypothesis_index=0), None
