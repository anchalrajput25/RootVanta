"""Incident memory: what past incidents turned out to be, and whether their fixes worked.

When an investigation names a cause, it is remembered. When the fix for it
finishes, the result is added. A new investigation then tells people when the
same thing happened before, what fixed it, and warns when the fix it is about
to propose already failed once for this cause.

Memory only informs people. It never changes a cause's confidence or the fix,
so one wrong past incident cannot steer the agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from incident_agent.db.models import IncidentMemoryRow, IncidentRow
from incident_agent.investigation.models import Investigation

MAX_SIMILAR = 5
OUTCOME_WORDS = {
    "verified": "worked and was proven",
    "partially_verified": "mostly worked",
    "unverified": "was applied but not proven",
    "rolled_back": "did not work and was undone",
    "apply_failed": "could not be applied",
    "undo_failed": "failed and could not be undone",
    "interrupted": "was interrupted",
}
FAILED = {"rolled_back", "apply_failed", "undo_failed"}
CAUSE_WORDS = {"bad_deploy": "bad deploy", "dependency_failure": "failing dependency", "saturation": "overload"}


@dataclass(frozen=True)
class Similar:
    incident_id: str
    title: str
    at: datetime
    match: str  # "same cause" or "same service"
    cause: str
    cause_service: str
    statement: str
    fix: Optional[dict]
    outcome: Optional[str]

    @property
    def fix_text(self) -> str:
        if not self.fix:
            return "no fix was proposed"
        what = f"{self.fix['type'].replace('_', ' ')} on {self.fix['service']}"
        return f"{what} {OUTCOME_WORDS.get(self.outcome or '', 'was not run')}"


def remember(db: Session, org_id: str, incident: IncidentRow, investigation_id: str, result: Investigation,
             plan_id: Optional[str]) -> None:
    if result.conclusion != "cause_identified" or not result.hypotheses:
        return
    top = result.hypotheses[0]
    fix = None
    if result.proposal is not None:
        a = result.proposal.action
        fix = {"type": a.type.value, "service": a.service, "params": dict(a.params)}
    db.add(IncidentMemoryRow(org_id=org_id, incident_id=incident.id, investigation_id=investigation_id,
                             service=incident.service, cause=top.cause.value, cause_service=top.service,
                             statement=top.statement[:500], fix=fix, plan_id=plan_id))


def record_outcome(db: Session, org_id: str, plan_id: str, outcome: str) -> None:
    for row in db.scalars(select(IncidentMemoryRow).where(IncidentMemoryRow.org_id == org_id,
                                                          IncidentMemoryRow.plan_id == plan_id)):
        row.outcome = outcome


def similar(db: Session, org_id: str, service: str, cause: Optional[str] = None,
            cause_service: Optional[str] = None, exclude_incident: Optional[str] = None) -> list[Similar]:
    """Past incidents like this one: the same cause in the same place first, then the same alerting service."""
    match = IncidentMemoryRow.service == service
    if cause and cause_service:
        match = or_(match, (IncidentMemoryRow.cause == cause) & (IncidentMemoryRow.cause_service == cause_service))
    stmt = (select(IncidentMemoryRow, IncidentRow.title).join(IncidentRow, IncidentRow.id == IncidentMemoryRow.incident_id)
            .where(IncidentMemoryRow.org_id == org_id, match)
            .order_by(IncidentMemoryRow.created_at.desc()).limit(100))
    if exclude_incident:
        stmt = stmt.where(IncidentMemoryRow.incident_id != exclude_incident)
    found: list[Similar] = []
    seen: set[str] = set()
    for row, title in db.execute(stmt):
        if row.incident_id in seen:
            continue  # several investigations of one incident: keep the latest
        seen.add(row.incident_id)
        same_cause = row.cause == cause and row.cause_service == cause_service
        found.append(Similar(incident_id=row.incident_id, title=title, at=row.created_at,
                             match="same cause" if same_cause else "same service", cause=row.cause,
                             cause_service=row.cause_service, statement=row.statement, fix=row.fix,
                             outcome=row.outcome))
    found.sort(key=lambda s: s.match != "same cause")  # stable: newest first within each group
    return found[:MAX_SIMILAR]


def notes(result: Investigation, past: list[Similar]) -> list[str]:
    """Lines for the report and the chat message."""
    same = [p for p in past if p.match == "same cause"]
    if not same:
        return []
    top = result.hypotheses[0]
    cause = f"{CAUSE_WORDS.get(top.cause.value, top.cause.value)} in {top.service}"
    last = same[0]
    out = [f"Seen before: {len(same)} earlier incident{'s' if len(same) > 1 else ''} with the same cause "
           f"({cause}). Last time ({last.at:%Y-%m-%d}): {last.fix_text}."]
    if result.proposal is not None:
        a = result.proposal.action
        failed = [p for p in same if p.fix and p.fix["type"] == a.type.value and p.fix["service"] == a.service
                  and p.outcome in FAILED]
        if failed:
            out.append(f"Warning: the same fix ({a.type.value.replace('_', ' ')} on {a.service}) "
                       f"{OUTCOME_WORDS[failed[0].outcome]} on {failed[0].at:%Y-%m-%d}. Check before approving.")
    return out
