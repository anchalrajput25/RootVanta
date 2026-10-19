"""Incidents, recovery plans, approvals and recovery verification."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from incident_agent.api.audit import record
from incident_agent.api.deps import Caller, get_caller, load_domain_user
from incident_agent.api.schemas import (
    ApprovalIn,
    ApprovalOut,
    IncidentIn,
    IncidentOut,
    PlanIn,
    PlanOut,
    VerificationIn,
    VerificationOut,
)
from incident_agent.db.models import (
    ApprovalRow,
    IncidentRow,
    RecoveryPlanRow,
    TeamRow,
    UserRow,
    VerificationRow,
)
from incident_agent.domain.models import (
    ActionSpec,
    Approval,
    ApprovalDecision,
    Environment,
    IncidentStatus,
    RecoveryPlan,
    Role,
    utcnow,
)
from incident_agent.safety.policy import PolicyDecision, authorize_execution
from incident_agent.workflow.plans import AWAITING_APPROVAL, READY, REJECTED, create_plan
from incident_agent.verification.evaluator import VerificationStatus, evaluate_recovery

router = APIRouter(tags=["incidents"])



def _require_team_access(caller: Caller, team_id: str) -> None:
    if team_id not in caller.user.team_ids and not caller.user.has_role(Role.ADMIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you are not a member of this team")


# --- incidents ----------------------------------------------------------------


@router.post("/incidents", response_model=IncidentOut, status_code=status.HTTP_201_CREATED)
def create_incident(body: IncidentIn, caller: Caller = Depends(get_caller)) -> IncidentRow:
    caller.require(Role.RESPONDER, Role.APPROVER)
    caller.get(TeamRow, body.team_id)
    _require_team_access(caller, body.team_id)
    row = IncidentRow(
        org_id=caller.org_id,
        team_id=body.team_id,
        title=body.title,
        service=body.service,
        environment=body.environment.value,
        severity=body.severity.value,
        status=IncidentStatus.OPEN.value,
        started_at=body.started_at,
        created_by=caller.user.id,
    )
    caller.db.add(row)
    caller.db.flush()
    record(caller.db, caller.org_id, caller.user.id, "incident.created", "incident", row.id,
           {"title": row.title, "service": row.service, "severity": row.severity})
    caller.db.commit()
    return row


@router.get("/incidents", response_model=list[IncidentOut])
def list_incidents(
    caller: Caller = Depends(get_caller),
    status_filter: Optional[IncidentStatus] = Query(None, alias="status"),
    team_id: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[IncidentRow]:
    stmt = select(IncidentRow).where(IncidentRow.org_id == caller.org_id)
    if status_filter:
        stmt = stmt.where(IncidentRow.status == status_filter.value)
    if team_id:
        stmt = stmt.where(IncidentRow.team_id == team_id)
    return list(caller.db.scalars(stmt.order_by(IncidentRow.started_at.desc()).limit(limit)))


@router.get("/incidents/{incident_id}", response_model=IncidentOut)
def get_incident(incident_id: str, caller: Caller = Depends(get_caller)) -> IncidentRow:
    return caller.get(IncidentRow, incident_id)


# --- recovery plans -------------------------------------------------------------


@router.post("/incidents/{incident_id}/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def propose_plan(incident_id: str, body: PlanIn, caller: Caller = Depends(get_caller)) -> RecoveryPlanRow:
    """Propose a recovery action. The safety policy decides immediately whether it may ever run."""
    incident = caller.get(IncidentRow, incident_id)
    action = ActionSpec(service=incident.service, environment=Environment(incident.environment),
                        **body.action.model_dump())
    row = create_plan(caller.db, incident, action, body.expected_effect, caller.user)
    caller.db.commit()
    return row


@router.get("/plans/{plan_id}", response_model=PlanOut)
def get_plan(plan_id: str, caller: Caller = Depends(get_caller)) -> RecoveryPlanRow:
    return caller.get(RecoveryPlanRow, plan_id)


def _domain_plan(row: RecoveryPlanRow) -> RecoveryPlan:
    return RecoveryPlan(
        id=row.id,
        org_id=row.org_id,
        team_id=row.team_id,
        incident_id=row.incident_id,
        action=ActionSpec.model_validate(row.action),
        expected_effect=row.expected_effect,
        requested_by=row.requested_by,
    )


@router.post("/plans/{plan_id}/approvals", response_model=ApprovalOut, status_code=status.HTTP_201_CREATED)
def decide_plan(plan_id: str, body: ApprovalIn, caller: Caller = Depends(get_caller)) -> ApprovalRow:
    caller.require(Role.APPROVER)
    # Lock the plan row so two approvers deciding at once are applied one after the other.
    row = caller.db.scalar(
        select(RecoveryPlanRow)
        .where(RecoveryPlanRow.id == plan_id, RecoveryPlanRow.org_id == caller.org_id)
        .with_for_update()
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "recovery_plan not found")
    if row.status != AWAITING_APPROVAL:
        raise HTTPException(status.HTTP_409_CONFLICT, f"plan is {row.status}, not awaiting approval")
    if row.requested_by == caller.user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you cannot approve your own request")

    approval = ApprovalRow(
        org_id=caller.org_id, plan_id=row.id, approver_id=caller.user.id,
        decision=body.decision.value, comment=body.comment,
    )
    caller.db.add(approval)
    try:
        caller.db.flush()
    except IntegrityError:
        caller.db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "you already decided on this plan") from None

    approvals = list(caller.db.scalars(select(ApprovalRow).where(ApprovalRow.plan_id == row.id)))
    approver_rows = caller.db.scalars(select(UserRow).where(UserRow.id.in_({a.approver_id for a in approvals})))
    users = {u.id: load_domain_user(caller.db, u) for u in approver_rows}
    auth = authorize_execution(
        _domain_plan(row),
        PolicyDecision.model_validate(row.policy_decision),
        [
            Approval(id=a.id, org_id=a.org_id, plan_id=a.plan_id, approver_id=a.approver_id,
                     decision=ApprovalDecision(a.decision))
            for a in approvals
        ],
        users,
    )
    if body.decision == ApprovalDecision.REJECTED:
        row.status = REJECTED
    elif auth.authorized:
        row.status = READY
    record(caller.db, caller.org_id, caller.user.id, f"plan.{body.decision.value}", "recovery_plan", row.id,
           {"comment": body.comment, "plan_status": row.status, "reason": auth.reason})
    caller.db.commit()
    return approval


# --- verification ------------------------------------------------------------------


@router.post("/plans/{plan_id}/verifications", response_model=VerificationOut, status_code=status.HTTP_201_CREATED)
def verify_plan(plan_id: str, body: VerificationIn, caller: Caller = Depends(get_caller)) -> VerificationRow:
    """Judge whether the executed plan actually restored the service, from evidence only."""
    caller.require(Role.RESPONDER, Role.APPROVER)
    plan = caller.get(RecoveryPlanRow, plan_id)
    if plan.status != READY:
        raise HTTPException(status.HTTP_409_CONFLICT, f"plan is {plan.status}; only an authorized plan can be verified")

    verdict = evaluate_recovery(
        criteria=body.criteria,
        observations=body.observations,
        test_results=body.test_results,
        action_completed_at=body.action_completed_at,
        evaluated_at=body.evaluated_at or utcnow(),
        agent_claim=body.agent_claim,
    )
    stored = verdict.model_dump(mode="json")
    stored["should_roll_back"] = verdict.should_roll_back
    row = VerificationRow(
        org_id=caller.org_id, plan_id=plan.id, status=verdict.status.value,
        verdict=stored, requested_by=caller.user.id,
    )
    caller.db.add(row)

    incident = caller.get(IncidentRow, plan.incident_id)
    incident.status = (
        IncidentStatus.RESOLVED.value if verdict.status == VerificationStatus.VERIFIED
        else IncidentStatus.MITIGATING.value
    )
    caller.db.flush()
    record(caller.db, caller.org_id, caller.user.id, f"verification.{verdict.status.value}", "recovery_plan",
           plan.id, {"verification_id": row.id, "summary": verdict.summary})
    caller.db.commit()
    return row


@router.get("/plans/{plan_id}/verifications", response_model=list[VerificationOut])
def list_verifications(plan_id: str, caller: Caller = Depends(get_caller)) -> list[VerificationRow]:
    plan = caller.get(RecoveryPlanRow, plan_id)
    stmt = select(VerificationRow).where(VerificationRow.plan_id == plan.id, VerificationRow.org_id == caller.org_id)
    return list(caller.db.scalars(stmt.order_by(VerificationRow.created_at)))
