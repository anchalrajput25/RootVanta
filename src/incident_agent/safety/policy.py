"""Deterministic safety policy.

The language model may *propose* a recovery plan, but only this module decides
whether it may run. Every rule is plain code, every decision lists the rules it
evaluated, and anything not explicitly allowed is denied.
"""

from __future__ import annotations

from typing import Iterable, Optional

from pydantic import BaseModel, ConfigDict, Field

from incident_agent.domain.models import (
    ActionType,
    Approval,
    ApprovalDecision,
    Environment,
    RecoveryPlan,
    RiskLevel,
    Role,
    User,
)

POLICY_VERSION = "2026-10-12.1"


class OrgPolicy(BaseModel):
    """Per-organization safety configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: str
    allowed_actions: dict[Environment, frozenset[ActionType]]
    max_blast_radius_pct: dict[Environment, float] = Field(
        default_factory=lambda: {
            Environment.DEV: 100.0,
            Environment.STAGING: 100.0,
            Environment.PRODUCTION: 50.0,
        }
    )
    max_replicas: int = 20
    # Actions at or above this risk need approval in any environment.
    approval_risk_threshold: RiskLevel = RiskLevel.MEDIUM
    # Every production action needs approval.
    production_requires_approval: bool = True

    @classmethod
    def default(cls, org_id: str) -> "OrgPolicy":
        safe = frozenset(
            {
                ActionType.ROLLBACK_DEPLOYMENT,
                ActionType.RESTART_SERVICE,
                ActionType.SCALE_SERVICE,
                ActionType.TOGGLE_FEATURE_FLAG,
            }
        )
        return cls(
            org_id=org_id,
            allowed_actions={
                Environment.DEV: safe,
                Environment.STAGING: safe,
                Environment.PRODUCTION: safe,
            },
        )


class RuleResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_id: str
    passed: bool
    reason: str


class PolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan_id: str
    allowed: bool
    requires_approval: bool
    results: tuple[RuleResult, ...]
    policy_version: str = POLICY_VERSION

    @property
    def violations(self) -> list[RuleResult]:
        return [r for r in self.results if not r.passed]


def evaluate_plan(plan: RecoveryPlan, requester: User, policy: OrgPolicy) -> PolicyDecision:
    """Check whether ``plan`` may be executed at all. Pure and deterministic."""
    action = plan.action
    results: list[RuleResult] = []

    def rule(rule_id: str, passed: bool, ok: str, fail: str) -> None:
        results.append(RuleResult(rule_id=rule_id, passed=passed, reason=ok if passed else fail))

    rule(
        "tenant.match",
        requester.org_id == plan.org_id == policy.org_id,
        "requester, plan and policy belong to the same organization",
        "requester, plan and policy belong to different organizations",
    )
    rule(
        "tenant.team_member",
        plan.team_id in requester.team_ids or requester.has_role(Role.ADMIN),
        "requester is a member of the plan's team",
        f"requester is not a member of team {plan.team_id}",
    )
    rule(
        "role.can_request",
        requester.has_role(Role.RESPONDER, Role.APPROVER),
        "requester may request recovery actions",
        "viewers cannot request recovery actions",
    )

    allowed_here = policy.allowed_actions.get(action.environment, frozenset())
    rule(
        "action.allowlisted",
        action.type in allowed_here,
        f"{action.type.value} is allowed in {action.environment.value}",
        f"{action.type.value} is not on the allowlist for {action.environment.value}",
    )

    is_prod = action.environment == Environment.PRODUCTION
    rule(
        "action.reversible",
        not is_prod or action.reversible or action.rollback is not None,
        "action is reversible or has a rollback plan",
        "production actions must be reversible or define a rollback",
    )

    limit = policy.max_blast_radius_pct.get(action.environment, 0.0)
    rule(
        "action.blast_radius",
        action.blast_radius_pct <= limit,
        f"blast radius {action.blast_radius_pct:g}% is within {limit:g}%",
        f"blast radius {action.blast_radius_pct:g}% exceeds the {limit:g}% limit",
    )

    if action.type == ActionType.SCALE_SERVICE:
        replicas = action.params.get("replicas")
        valid = isinstance(replicas, int) and 1 <= replicas <= policy.max_replicas
        rule(
            "action.scale_bounds",
            valid,
            f"target of {replicas} replicas is within 1..{policy.max_replicas}",
            f"target replicas {replicas!r} is outside 1..{policy.max_replicas}",
        )

    if action.type == ActionType.ROLLBACK_DEPLOYMENT:
        target = action.params.get("target_version")
        rule(
            "action.rollback_target",
            isinstance(target, str) and bool(target.strip()),
            f"rollback target is {target}",
            "rollback needs an explicit target_version",
        )

    requires_approval = (
        (is_prod and policy.production_requires_approval)
        or action.risk.rank >= policy.approval_risk_threshold.rank
    )
    allowed = all(r.passed for r in results)
    return PolicyDecision(
        plan_id=plan.id,
        allowed=allowed,
        requires_approval=requires_approval,
        results=tuple(results),
    )


class ExecutionAuthorization(BaseModel):
    model_config = ConfigDict(frozen=True)

    authorized: bool
    reason: str
    approval_id: Optional[str] = None


def authorize_execution(
    plan: RecoveryPlan,
    decision: PolicyDecision,
    approvals: Iterable[Approval],
    users: dict[str, User],
) -> ExecutionAuthorization:
    """Final gate before an executor may run ``plan``.

    Requires a passing policy decision for this exact plan and, when approval is
    needed, an approval from a different person in the same organization who
    holds the approver role. Any rejection blocks execution.
    """
    if decision.plan_id != plan.id:
        return ExecutionAuthorization(authorized=False, reason="policy decision is for a different plan")
    if not decision.allowed:
        failed = ", ".join(r.rule_id for r in decision.violations)
        return ExecutionAuthorization(authorized=False, reason=f"blocked by policy: {failed}")
    if not decision.requires_approval:
        return ExecutionAuthorization(authorized=True, reason="no approval required")

    relevant = [a for a in approvals if a.plan_id == plan.id and a.org_id == plan.org_id]
    if any(a.decision == ApprovalDecision.REJECTED for a in relevant):
        return ExecutionAuthorization(authorized=False, reason="plan was rejected by an approver")

    for approval in relevant:
        approver = users.get(approval.approver_id)
        if approver is None or approver.org_id != plan.org_id:
            continue
        if approver.id == plan.requested_by:
            continue  # nobody approves their own request
        if not approver.has_role(Role.APPROVER):
            continue
        return ExecutionAuthorization(
            authorized=True, reason=f"approved by {approver.email}", approval_id=approval.id
        )
    return ExecutionAuthorization(
        authorized=False, reason="needs approval from a different user with the approver role"
    )
