import pytest

from incident_agent.domain import (
    ActionSpec,
    ActionType,
    Approval,
    ApprovalDecision,
    Environment,
    RecoveryPlan,
    RiskLevel,
    Role,
    User,
)
from incident_agent.safety import OrgPolicy, authorize_execution, evaluate_plan

ORG, OTHER_ORG, TEAM = "org_acme", "org_globex", "team_payments"


def user(uid, *roles, org=ORG, teams=(TEAM,)):
    return User(id=uid, org_id=org, team_ids=frozenset(teams), email=f"{uid}@example.com", roles=frozenset(roles))


RESPONDER = user("alice", Role.RESPONDER)
APPROVER = user("bob", Role.APPROVER)
VIEWER = user("carol", Role.VIEWER)
FOREIGN_APPROVER = user("mallory", Role.APPROVER, org=OTHER_ORG)
USERS = {u.id: u for u in (RESPONDER, APPROVER, VIEWER, FOREIGN_APPROVER)}
POLICY = OrgPolicy.default(ORG)


def plan(action_type=ActionType.ROLLBACK_DEPLOYMENT, env=Environment.PRODUCTION, *, params=None,
         risk=RiskLevel.MEDIUM, reversible=True, blast=25.0, org=ORG, requested_by="alice"):
    if params is None:
        params = {"target_version": "v1.2.3"} if action_type == ActionType.ROLLBACK_DEPLOYMENT else {}
    return RecoveryPlan(
        org_id=org, team_id=TEAM, incident_id="inc_1", requested_by=requested_by, expected_effect="recover",
        action=ActionSpec(type=action_type, service="checkout", environment=env, params=params,
                          risk=risk, reversible=reversible, blast_radius_pct=blast),
    )


def approve(p, approver, decision=ApprovalDecision.APPROVED):
    return Approval(org_id=p.org_id, plan_id=p.id, approver_id=approver.id, decision=decision)


def failed_rules(decision):
    return {r.rule_id for r in decision.violations}


def test_valid_production_rollback_is_allowed_but_needs_approval():
    d = evaluate_plan(plan(), RESPONDER, POLICY)
    assert d.allowed and d.requires_approval


def test_unlisted_destructive_action_is_blocked():
    d = evaluate_plan(plan(ActionType.DELETE_RESOURCE, risk=RiskLevel.HIGH, reversible=False), RESPONDER, POLICY)
    assert not d.allowed
    assert "action.allowlisted" in failed_rules(d)


def test_arbitrary_shell_is_never_allowed_by_default():
    d = evaluate_plan(plan(ActionType.RUN_SHELL, env=Environment.DEV, risk=RiskLevel.LOW), RESPONDER, POLICY)
    assert not d.allowed


def test_viewer_cannot_request_actions():
    d = evaluate_plan(plan(requested_by="carol"), VIEWER, POLICY)
    assert "role.can_request" in failed_rules(d)


def test_cross_tenant_request_is_blocked():
    outsider = user("eve", Role.RESPONDER, org=OTHER_ORG)
    d = evaluate_plan(plan(), outsider, POLICY)
    assert "tenant.match" in failed_rules(d)


def test_non_team_member_is_blocked():
    other_team = user("dan", Role.RESPONDER, teams=("team_search",))
    d = evaluate_plan(plan(), other_team, POLICY)
    assert "tenant.team_member" in failed_rules(d)


def test_irreversible_production_action_is_blocked():
    d = evaluate_plan(plan(ActionType.RESTART_SERVICE, reversible=False), RESPONDER, POLICY)
    assert "action.reversible" in failed_rules(d)


def test_blast_radius_limit_in_production():
    d = evaluate_plan(plan(blast=80), RESPONDER, POLICY)
    assert "action.blast_radius" in failed_rules(d)


@pytest.mark.parametrize("replicas", [0, 21, "8", None])
def test_scale_bounds(replicas):
    d = evaluate_plan(plan(ActionType.SCALE_SERVICE, params={"replicas": replicas}), RESPONDER, POLICY)
    assert "action.scale_bounds" in failed_rules(d)


def test_rollback_needs_explicit_target():
    d = evaluate_plan(plan(params={}), RESPONDER, POLICY)
    assert "action.rollback_target" in failed_rules(d)


def test_low_risk_dev_action_runs_without_approval():
    p = plan(ActionType.RESTART_SERVICE, env=Environment.DEV, risk=RiskLevel.LOW)
    d = evaluate_plan(p, RESPONDER, POLICY)
    assert d.allowed and not d.requires_approval
    assert authorize_execution(p, d, [], USERS).authorized


def test_execution_requires_an_approver():
    p = plan()
    d = evaluate_plan(p, RESPONDER, POLICY)
    assert not authorize_execution(p, d, [], USERS).authorized
    assert authorize_execution(p, d, [approve(p, APPROVER)], USERS).authorized


def test_nobody_approves_their_own_request():
    self_approver = user("alice", Role.RESPONDER, Role.APPROVER)
    p = plan()
    d = evaluate_plan(p, self_approver, POLICY)
    auth = authorize_execution(p, d, [approve(p, self_approver)], {self_approver.id: self_approver})
    assert not auth.authorized


def test_approver_from_another_company_does_not_count():
    p = plan()
    d = evaluate_plan(p, RESPONDER, POLICY)
    assert not authorize_execution(p, d, [approve(p, FOREIGN_APPROVER)], USERS).authorized


def test_viewer_approval_does_not_count():
    p = plan()
    d = evaluate_plan(p, RESPONDER, POLICY)
    assert not authorize_execution(p, d, [approve(p, VIEWER)], USERS).authorized


def test_rejection_blocks_even_with_an_approval():
    p = plan()
    d = evaluate_plan(p, RESPONDER, POLICY)
    other_approver = user("frank", Role.APPROVER)
    users = {**USERS, other_approver.id: other_approver}
    approvals = [approve(p, APPROVER), approve(p, other_approver, ApprovalDecision.REJECTED)]
    assert not authorize_execution(p, d, approvals, users).authorized


def test_approval_cannot_unlock_a_blocked_plan():
    p = plan(ActionType.DELETE_RESOURCE, risk=RiskLevel.HIGH, reversible=False)
    d = evaluate_plan(p, RESPONDER, POLICY)
    assert not authorize_execution(p, d, [approve(p, APPROVER)], USERS).authorized


def test_decision_for_another_plan_is_rejected():
    p1, p2 = plan(), plan()
    d1 = evaluate_plan(p1, RESPONDER, POLICY)
    assert not authorize_execution(p2, d1, [approve(p2, APPROVER)], USERS).authorized
