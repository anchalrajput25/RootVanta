"""Evidence-based recovery verification.

Decides whether a recovery action actually worked, from telemetry samples and
test results only. Model-written text never changes the outcome, and missing
data is never treated as success.

Status precedence:
    FAILED              any required criterion or any regression test failed
    UNVERIFIED          otherwise, some required check has no data or its
                        observation window has not finished
    PARTIALLY_VERIFIED  all required checks passed, but an optional one
                        failed or had no data
    VERIFIED            every check passed with full window coverage
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from statistics import fmean
from typing import Iterable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class CriterionKind(str, Enum):
    SYMPTOM = "symptom"  # the signal that triggered the incident
    SLI = "sli"  # a service-level indicator that must stay healthy
    REGRESSION = "regression"  # a signal that must not get worse


class Comparator(str, Enum):
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"

    def holds(self, value: float, threshold: float) -> bool:
        if self is Comparator.LT:
            return value < threshold
        if self is Comparator.LTE:
            return value <= threshold
        if self is Comparator.GT:
            return value > threshold
        return value >= threshold


class SuccessCriterion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    signal: str
    comparator: Comparator
    threshold: float
    window_seconds: int = Field(gt=0)
    kind: CriterionKind = CriterionKind.SLI
    required: bool = True
    # "all": every sample in the window must satisfy the comparison.
    # "mean": the window average must satisfy it.
    aggregation: Literal["all", "mean"] = "all"
    min_samples: int = Field(default=3, ge=1)


class Observation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    signal: str
    at: datetime
    value: float
    evidence_ref: Optional[str] = None


class TestResult(BaseModel):
    __test__ = False  # not a pytest test class

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    passed: Optional[bool]  # None means the test did not run
    required: bool = True
    evidence_ref: Optional[str] = None


class Outcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NO_DATA = "no_data"
    WINDOW_INCOMPLETE = "window_incomplete"


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    FAILED = "failed"
    UNVERIFIED = "unverified"


class CheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    check_id: str
    kind: str
    required: bool
    outcome: Outcome
    reason: str
    observed: Optional[float] = None
    samples: int = 0
    evidence_refs: tuple[str, ...] = ()


class Verdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: VerificationStatus
    checks: tuple[CheckResult, ...]
    summary: str
    evaluated_at: datetime
    action_completed_at: datetime
    # Kept for the audit trail only; it is never read when deciding status.
    agent_claim: Optional[str] = None

    @property
    def recovered(self) -> bool:
        return self.status == VerificationStatus.VERIFIED

    @property
    def should_roll_back(self) -> bool:
        return self.status == VerificationStatus.FAILED


def _check_criterion(
    criterion: SuccessCriterion,
    observations: list[Observation],
    action_completed_at: datetime,
    evaluated_at: datetime,
) -> CheckResult:
    window_start = action_completed_at
    window_end = action_completed_at + timedelta(seconds=criterion.window_seconds)
    in_window = sorted(
        (
            o
            for o in observations
            if o.signal == criterion.signal and window_start <= o.at <= window_end and o.at <= evaluated_at
        ),
        key=lambda o: o.at,
    )
    refs = tuple(o.evidence_ref for o in in_window if o.evidence_ref)
    base = dict(
        check_id=criterion.id,
        kind=criterion.kind.value,
        required=criterion.required,
        samples=len(in_window),
        evidence_refs=refs,
    )
    rule = f"{criterion.signal} {criterion.comparator.value} {criterion.threshold:g}"

    # A single bad sample already proves an "all" criterion failed, even
    # before the window is over.
    if criterion.aggregation == "all":
        bad = [o for o in in_window if not criterion.comparator.holds(o.value, criterion.threshold)]
        if bad:
            return CheckResult(
                **base,
                outcome=Outcome.FAIL,
                observed=bad[0].value,
                reason=f"{criterion.signal} was {bad[0].value:g} at {bad[0].at.isoformat()}, needs {rule}",
            )

    if evaluated_at < window_end:
        return CheckResult(
            **base,
            outcome=Outcome.WINDOW_INCOMPLETE,
            reason=f"observation window ends at {window_end.isoformat()}, not reached yet",
        )

    if len(in_window) < criterion.min_samples:
        return CheckResult(
            **base,
            outcome=Outcome.NO_DATA,
            reason=f"only {len(in_window)} sample(s) of {criterion.signal} in the window, "
            f"need at least {criterion.min_samples}",
        )

    if criterion.aggregation == "mean":
        avg = fmean(o.value for o in in_window)
        ok = criterion.comparator.holds(avg, criterion.threshold)
        return CheckResult(
            **base,
            outcome=Outcome.PASS if ok else Outcome.FAIL,
            observed=avg,
            reason=f"window mean {avg:g}, needs {rule}",
        )

    worst = max(in_window, key=lambda o: o.value) if criterion.comparator in (Comparator.LT, Comparator.LTE) else min(
        in_window, key=lambda o: o.value
    )
    return CheckResult(
        **base,
        outcome=Outcome.PASS,
        observed=worst.value,
        reason=f"all {len(in_window)} samples satisfy {rule} (worst {worst.value:g})",
    )


def _check_test(test: TestResult) -> CheckResult:
    refs = (test.evidence_ref,) if test.evidence_ref else ()
    if test.passed is None:
        outcome, reason = Outcome.NO_DATA, f"test '{test.name}' did not run"
    elif test.passed:
        outcome, reason = Outcome.PASS, f"test '{test.name}' passed"
    else:
        outcome, reason = Outcome.FAIL, f"test '{test.name}' failed"
    return CheckResult(
        check_id=test.id,
        kind="regression_test",
        required=test.required,
        outcome=outcome,
        reason=reason,
        evidence_refs=refs,
    )


def evaluate_recovery(
    criteria: Iterable[SuccessCriterion],
    observations: Iterable[Observation],
    test_results: Iterable[TestResult],
    action_completed_at: datetime,
    evaluated_at: datetime,
    agent_claim: Optional[str] = None,
) -> Verdict:
    """Return a verdict on whether recovery is supported by evidence."""
    criteria = list(criteria)
    if not criteria:
        raise ValueError("at least one success criterion is required")
    obs = list(observations)

    checks = [_check_criterion(c, obs, action_completed_at, evaluated_at) for c in criteria]
    checks += [_check_test(t) for t in test_results]

    required = [c for c in checks if c.required]
    optional = [c for c in checks if not c.required]
    regression_failed = [
        c for c in checks if c.outcome == Outcome.FAIL and c.kind in ("regression", "regression_test")
    ]
    required_failed = [c for c in required if c.outcome == Outcome.FAIL]
    required_unknown = [c for c in required if c.outcome in (Outcome.NO_DATA, Outcome.WINDOW_INCOMPLETE)]
    optional_not_passed = [c for c in optional if c.outcome != Outcome.PASS]

    if required_failed or regression_failed:
        status = VerificationStatus.FAILED
        failed = required_failed + [c for c in regression_failed if c not in required_failed]
        summary = "Recovery failed: " + "; ".join(c.reason for c in failed)
    elif required_unknown:
        status = VerificationStatus.UNVERIFIED
        summary = "Recovery not verified: " + "; ".join(c.reason for c in required_unknown)
    elif optional_not_passed:
        status = VerificationStatus.PARTIALLY_VERIFIED
        summary = "Required checks passed, but: " + "; ".join(c.reason for c in optional_not_passed)
    else:
        status = VerificationStatus.VERIFIED
        summary = f"All {len(checks)} checks passed"

    return Verdict(
        status=status,
        checks=tuple(checks),
        summary=summary,
        evaluated_at=evaluated_at,
        action_completed_at=action_completed_at,
        agent_claim=agent_claim,
    )
