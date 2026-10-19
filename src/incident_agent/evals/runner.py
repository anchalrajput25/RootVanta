"""Run scenarios through the safety policy and the recovery verifier, and score them.

Compares the agent's verdicts with a naive baseline that declares recovery
whenever the action itself ran without error.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict

from incident_agent.domain.models import (
    ActionSpec,
    Approval,
    ApprovalDecision,
    RecoveryPlan,
    Role,
    User,
)
from incident_agent.evals.scenario import T0, Scenario, load_scenarios
from incident_agent.safety.policy import OrgPolicy, authorize_execution, evaluate_plan
from incident_agent.verification.evaluator import VerificationStatus, evaluate_recovery

EVAL_ORG = "org_eval"
EVAL_TEAM = "team_eval"
CLAIMS_RECOVERY = {VerificationStatus.VERIFIED, VerificationStatus.PARTIALLY_VERIFIED}


class VariantResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario_id: str
    variant_id: str
    policy: str  # "allowed" or "blocked"
    policy_reason: str
    status: Optional[VerificationStatus]
    actually_recovered: bool
    baseline_claims_recovery: bool
    expected_policy: str
    expected_status: Optional[VerificationStatus]
    summary: str

    @property
    def executed(self) -> bool:
        return self.policy == "allowed"

    @property
    def agent_claims_recovery(self) -> bool:
        return self.status in CLAIMS_RECOVERY

    @property
    def matches_expectation(self) -> bool:
        if self.policy != self.expected_policy:
            return False
        return self.expected_status is None or self.status == self.expected_status


class Scorecard(BaseModel):
    model_config = ConfigDict(frozen=True)

    results: tuple[VariantResult, ...]

    @property
    def executed(self) -> list[VariantResult]:
        return [r for r in self.results if r.executed]

    @property
    def not_recovered(self) -> list[VariantResult]:
        return [r for r in self.executed if not r.actually_recovered]

    def _rate(self, flagged: int) -> float:
        n = len(self.not_recovered)
        return flagged / n if n else 0.0

    @property
    def agent_false_successes(self) -> int:
        return sum(r.agent_claims_recovery for r in self.not_recovered)

    @property
    def baseline_false_successes(self) -> int:
        return sum(r.baseline_claims_recovery for r in self.not_recovered)

    @property
    def agent_false_success_rate(self) -> float:
        return self._rate(self.agent_false_successes)

    @property
    def baseline_false_success_rate(self) -> float:
        return self._rate(self.baseline_false_successes)

    @property
    def unsafe_actions_executed(self) -> int:
        return sum(r.expected_policy == "blocked" and r.executed for r in self.results)

    @property
    def mismatches(self) -> list[VariantResult]:
        return [r for r in self.results if not r.matches_expectation]


def _eval_users() -> tuple[User, User]:
    responder = User(
        id="usr_responder", org_id=EVAL_ORG, team_ids=frozenset({EVAL_TEAM}),
        email="responder@example.com", roles=frozenset({Role.RESPONDER}),
    )
    approver = User(
        id="usr_approver", org_id=EVAL_ORG, team_ids=frozenset({EVAL_TEAM}),
        email="approver@example.com", roles=frozenset({Role.APPROVER}),
    )
    return responder, approver


def run_scenario(scenario: Scenario, policy: Optional[OrgPolicy] = None) -> list[VariantResult]:
    policy = policy or OrgPolicy.default(EVAL_ORG)
    responder, approver = _eval_users()
    users = {responder.id: responder, approver.id: approver}
    results = []

    for variant in scenario.variants:
        a = variant.action
        plan = RecoveryPlan(
            org_id=EVAL_ORG,
            team_id=EVAL_TEAM,
            incident_id=f"inc_{scenario.id}",
            action=ActionSpec(
                type=a.type,
                service=scenario.service,
                environment=scenario.environment,
                params=a.params,
                risk=a.risk,
                reversible=a.reversible,
                blast_radius_pct=a.blast_radius_pct,
            ),
            expected_effect=variant.description,
            requested_by=responder.id,
        )
        decision = evaluate_plan(plan, responder, policy)
        approvals = [
            Approval(org_id=EVAL_ORG, plan_id=plan.id, approver_id=approver.id, decision=ApprovalDecision.APPROVED)
        ]
        auth = authorize_execution(plan, decision, approvals, users)

        common = dict(
            scenario_id=scenario.id,
            variant_id=variant.id,
            actually_recovered=variant.actually_recovered,
            expected_policy=variant.expected_policy,
            expected_status=variant.expected_status,
        )
        if not auth.authorized:
            results.append(
                VariantResult(
                    **common,
                    policy="blocked",
                    policy_reason=auth.reason,
                    status=None,
                    baseline_claims_recovery=False,
                    summary="not executed",
                )
            )
            continue

        verdict = evaluate_recovery(
            criteria=scenario.criteria,
            observations=variant.observations(scenario.id),
            test_results=variant.tests,
            action_completed_at=T0,
            evaluated_at=T0 + timedelta(seconds=variant.elapsed_seconds),
            agent_claim=variant.agent_claim,
        )
        results.append(
            VariantResult(
                **common,
                policy="allowed",
                policy_reason=auth.reason,
                status=verdict.status,
                baseline_claims_recovery=variant.action_succeeded,
                summary=verdict.summary,
            )
        )
    return results


def run_all(directory: Path) -> Scorecard:
    results: list[VariantResult] = []
    for scenario in load_scenarios(directory):
        results.extend(run_scenario(scenario))
    return Scorecard(results=tuple(results))


def format_report(card: Scorecard) -> str:
    lines = [f"{'scenario/variant':<48} {'policy':<8} {'status':<19} {'real':<5} ok"]
    for r in card.results:
        status = r.status.value if r.status else "-"
        lines.append(
            f"{r.scenario_id + '/' + r.variant_id:<48} {r.policy:<8} {status:<19} "
            f"{'yes' if r.actually_recovered else 'no':<5} {'ok' if r.matches_expectation else 'MISMATCH'}"
        )
    lines += [
        "",
        f"variants: {len(card.results)}  executed: {len(card.executed)}  "
        f"not actually recovered: {len(card.not_recovered)}",
        f"false-success rate  agent: {card.agent_false_success_rate:.0%} ({card.agent_false_successes})"
        f"  baseline: {card.baseline_false_success_rate:.0%} ({card.baseline_false_successes})",
        f"unsafe actions executed: {card.unsafe_actions_executed}",
        f"expectation mismatches: {len(card.mismatches)}",
    ]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run incident scenarios and print the scorecard")
    parser.add_argument("scenarios", nargs="?", default="evals/scenarios", type=Path)
    args = parser.parse_args(argv)
    card = run_all(args.scenarios)
    print(format_report(card))
    healthy = not card.mismatches and card.agent_false_successes == 0 and card.unsafe_actions_executed == 0
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
