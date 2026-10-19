from datetime import datetime, timedelta, timezone

import pytest

from incident_agent.verification import (
    Comparator,
    CriterionKind,
    Observation,
    Outcome,
    SuccessCriterion,
    TestResult,
    VerificationStatus,
    evaluate_recovery,
)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
WINDOW = 600

ERROR_RATE = SuccessCriterion(
    id="error_rate", signal="http_5xx_rate", comparator=Comparator.LT, threshold=0.01,
    window_seconds=WINDOW, kind=CriterionKind.SYMPTOM,
)
LATENCY = SuccessCriterion(
    id="latency", signal="p95_latency_ms", comparator=Comparator.LT, threshold=400,
    window_seconds=WINDOW, aggregation="mean",
)
CPU_OPTIONAL = SuccessCriterion(
    id="cpu", signal="cpu_utilization", comparator=Comparator.LT, threshold=0.85,
    window_seconds=WINDOW, required=False, aggregation="mean",
)
CRITERIA = [ERROR_RATE, LATENCY, CPU_OPTIONAL]
REGRESSION_OK = TestResult(id="t1", name="checkout e2e", passed=True)


def series(signal, values, interval=60):
    return [Observation(signal=signal, at=T0 + timedelta(seconds=i * interval), value=v) for i, v in enumerate(values)]


def healthy(*, skip=()):
    obs = []
    if "http_5xx_rate" not in skip:
        obs += series("http_5xx_rate", [0.002] * 11)
    if "p95_latency_ms" not in skip:
        obs += series("p95_latency_ms", [280] * 11)
    if "cpu_utilization" not in skip:
        obs += series("cpu_utilization", [0.5] * 11)
    return obs


def verify(observations, tests=(REGRESSION_OK,), elapsed=WINDOW, **kw):
    return evaluate_recovery(
        criteria=CRITERIA,
        observations=observations,
        test_results=tests,
        action_completed_at=T0,
        evaluated_at=T0 + timedelta(seconds=elapsed),
        **kw,
    )


def test_rollback_works_is_verified():
    verdict = verify(healthy())
    assert verdict.status == VerificationStatus.VERIFIED
    assert verdict.recovered and not verdict.should_roll_back


def test_symptom_cleared_but_regression_test_fails_is_failed():
    failing = TestResult(id="t_coupon", name="coupon API", passed=False)
    verdict = verify(healthy(), tests=(failing,))
    assert verdict.status == VerificationStatus.FAILED
    assert verdict.should_roll_back
    assert "coupon API" in verdict.summary


def test_missing_symptom_telemetry_is_unverified_never_verified():
    verdict = verify(healthy(skip={"http_5xx_rate"}))
    assert verdict.status == VerificationStatus.UNVERIFIED
    check = next(c for c in verdict.checks if c.check_id == "error_rate")
    assert check.outcome == Outcome.NO_DATA


def test_incomplete_window_is_unverified_even_if_healthy_so_far():
    obs = [o for o in healthy() if o.at <= T0 + timedelta(seconds=120)]
    verdict = verify(obs, elapsed=120)
    assert verdict.status == VerificationStatus.UNVERIFIED
    assert any(c.outcome == Outcome.WINDOW_INCOMPLETE for c in verdict.checks)


def test_wrong_fix_fails_as_soon_as_errors_return():
    obs = healthy(skip={"http_5xx_rate"}) + series("http_5xx_rate", [0.002, 0.05, 0.2])
    verdict = verify(obs, elapsed=180)
    assert verdict.status == VerificationStatus.FAILED  # no need to wait out the window


def test_optional_signal_missing_is_partially_verified():
    verdict = verify(healthy(skip={"cpu_utilization"}))
    assert verdict.status == VerificationStatus.PARTIALLY_VERIFIED
    assert not verdict.recovered


def test_model_claim_cannot_change_the_outcome():
    obs = healthy(skip={"http_5xx_rate"})
    with_claim = verify(obs, agent_claim="The service has fully recovered.")
    without = verify(obs)
    assert with_claim.status == without.status == VerificationStatus.UNVERIFIED
    assert with_claim.agent_claim == "The service has fully recovered."


def test_same_inputs_give_identical_verdicts():
    obs = healthy()
    assert verify(obs).model_dump() == verify(list(reversed(obs))).model_dump()


def test_test_that_did_not_run_is_unverified():
    verdict = verify(healthy(), tests=(TestResult(id="t1", name="checkout e2e", passed=None),))
    assert verdict.status == VerificationStatus.UNVERIFIED


def test_regression_signal_failing_is_failed_even_when_optional():
    new_errors = SuccessCriterion(
        id="new_errors", signal="new_error_signatures", comparator=Comparator.LTE, threshold=0,
        window_seconds=WINDOW, kind=CriterionKind.REGRESSION, required=False,
    )
    verdict = evaluate_recovery(
        criteria=CRITERIA + [new_errors],
        observations=healthy() + series("new_error_signatures", [0, 0, 3] + [3] * 8),
        test_results=(REGRESSION_OK,),
        action_completed_at=T0,
        evaluated_at=T0 + timedelta(seconds=WINDOW),
    )
    assert verdict.status == VerificationStatus.FAILED


def test_samples_before_the_action_are_ignored():
    before = [Observation(signal="http_5xx_rate", at=T0 - timedelta(seconds=30), value=0.5)]
    verdict = verify(before + healthy())
    assert verdict.status == VerificationStatus.VERIFIED


def test_requires_at_least_one_criterion():
    with pytest.raises(ValueError):
        evaluate_recovery([], [], [], T0, T0)
