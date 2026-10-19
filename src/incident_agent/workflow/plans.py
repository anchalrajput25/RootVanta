"""Creating recovery plans, shared by the API (people) and the workers (the agent)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from incident_agent.api.audit import record
from incident_agent.db.models import IncidentRow, RecoveryPlanRow, TeamMemberRow, UserRow
from incident_agent.domain.models import ActionSpec, RecoveryPlan, Role, User
from incident_agent.safety.policy import OrgPolicy, evaluate_plan

# Plan lifecycle
BLOCKED = "blocked"
AWAITING_APPROVAL = "awaiting_approval"
READY = "ready"  # allowed by policy and, if needed, approved
REJECTED = "rejected"
QUEUED = "queued"  # waiting for a worker to apply it
EXECUTING = "executing"
# After execution the plan takes the execution's final status:
# verified, partially_verified, unverified, rolled_back, apply_failed, undo_failed, interrupted

AGENT_EMAIL = "agent+{org_id}@incident-agent.invalid"


def agent_user(db: Session, org_id: str, team_id: str) -> User:
    """The organization's agent account: it proposes fixes, people approve them.

    It holds only the responder role and has no usable password, so nobody can
    log in as it, and the rule that nobody approves their own request keeps a
    human in the loop."""
    email = AGENT_EMAIL.format(org_id=org_id)
    row = db.scalar(select(UserRow).where(UserRow.email == email, UserRow.org_id == org_id))
    if row is None:
        row = UserRow(org_id=org_id, email=email, password_hash="!", roles=[Role.RESPONDER.value])
        db.add(row)
        db.flush()
    member = db.get(TeamMemberRow, {"team_id": team_id, "user_id": row.id})
    if member is None:
        db.add(TeamMemberRow(org_id=org_id, team_id=team_id, user_id=row.id))
        db.flush()
    team_ids = db.scalars(select(TeamMemberRow.team_id).where(TeamMemberRow.user_id == row.id)).all()
    return User(id=row.id, org_id=org_id, email=email, team_ids=frozenset(team_ids),
                roles=frozenset({Role.RESPONDER}))


def create_plan(db: Session, incident: IncidentRow, action: ActionSpec, expected_effect: str,
                requester: User) -> RecoveryPlanRow:
    """Run the safety policy on a proposed action and store it. The caller commits."""
    plan = RecoveryPlan(org_id=incident.org_id, team_id=incident.team_id, incident_id=incident.id, action=action,
                        expected_effect=expected_effect, requested_by=requester.id)
    decision = evaluate_plan(plan, requester, OrgPolicy.default(incident.org_id))
    if not decision.allowed:
        status = BLOCKED
    elif decision.requires_approval:
        status = AWAITING_APPROVAL
    else:
        status = READY
    row = RecoveryPlanRow(
        id=plan.id, org_id=incident.org_id, team_id=plan.team_id, incident_id=incident.id,
        action=plan.action.model_dump(mode="json"), expected_effect=plan.expected_effect,
        requested_by=requester.id, policy_decision=decision.model_dump(mode="json"), status=status,
    )
    db.add(row)
    record(db, incident.org_id, requester.id, f"plan.{status}", "recovery_plan", row.id,
           {"action": row.action["type"], "violations": [v.rule_id for v in decision.violations]})
    return row
