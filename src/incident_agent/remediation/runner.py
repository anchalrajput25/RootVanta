"""Run an approved fix: practice run, apply in stages, check each stage, undo on failure.

    preview -> apply stage 1 (e.g. 10%) -> wait -> check for harm
            -> apply stage 2 (100%)    -> wait -> full recovery check
    a FAILED check at any stage -> undo the whole change, stop

The checks use the same verifier as everything else: real metrics only, and
missing data is never a success. A stage without enough data is not widened.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from incident_agent.domain.models import ActionSpec, ActionType
from incident_agent.remediation.executor import Executor, ExecutorError, Preview
from incident_agent.telemetry.prometheus import PrometheusClient, PrometheusError, collect_observations
from incident_agent.verification.evaluator import (
    Comparator,
    CriterionKind,
    SuccessCriterion,
    Verdict,
    VerificationStatus,
    evaluate_recovery,
)

SCRAPE_SLACK_SECONDS = 20  # let the last samples be scraped and recorded


@dataclass(frozen=True)
class Timing:
    settle_seconds: int = 60  # the signals are 1-minute rates
    window_seconds: int = 600  # full check after the last stage
    stage_window_seconds: int = 120  # harm check after a partial stage


# --- what "recovered" means ------------------------------------------------------------


def recovery_criteria(window_seconds: int, limits: Optional[dict[str, float]] = None) -> list[SuccessCriterion]:
    """The full check after the fix reaches everyone. Limits can be set per service in the runbook."""
    lim = {"max_5xx_rate": 0.01, "max_p95_ms": 400.0, **(limits or {})}
    return [
        SuccessCriterion(id="error_rate_recovered", signal="http_5xx_rate", comparator=Comparator.LT,
                         threshold=lim["max_5xx_rate"], window_seconds=window_seconds, kind=CriterionKind.SYMPTOM),
        SuccessCriterion(id="latency_healthy", signal="p95_latency_ms", comparator=Comparator.LT,
                         threshold=lim["max_p95_ms"], window_seconds=window_seconds, kind=CriterionKind.SLI,
                         aggregation="mean"),
        SuccessCriterion(id="no_new_errors", signal="new_error_signatures", comparator=Comparator.LTE,
                         threshold=0, window_seconds=window_seconds, kind=CriterionKind.REGRESSION),
        SuccessCriterion(id="cpu_headroom", signal="cpu_utilization", comparator=Comparator.LT, threshold=0.85,
                         window_seconds=window_seconds, kind=CriterionKind.SLI, required=False, aggregation="mean"),
    ]


def stage_criteria(window_seconds: int, error_rate_before: Optional[float]) -> list[SuccessCriterion]:
    """A partial stage only reaches some users, so it cannot be expected to fix
    everything. Its job is to catch new breakage before it reaches everyone.

    The overall error rate is reported but not required: at 10% it is mostly
    the other 90% of traffic, and during an incident it is still moving, so it
    cannot prove harm. The full check after the last stage judges recovery."""
    out = [SuccessCriterion(id="no_new_errors", signal="new_error_signatures", comparator=Comparator.LTE,
                            threshold=0, window_seconds=window_seconds, kind=CriterionKind.REGRESSION)]
    if error_rate_before is not None:
        out.append(SuccessCriterion(
            id="error_rate_not_worse", signal="http_5xx_rate", comparator=Comparator.LTE,
            threshold=round(error_rate_before * 1.1 + 0.02, 4), window_seconds=window_seconds,
            kind=CriterionKind.SLI, required=False, aggregation="mean"))  # a regression would always be required
    return out


def rollout_stages(action: ActionSpec) -> list[ActionSpec]:
    """A feature flag with rollout_pct < 100 goes to that share first, then to everyone."""
    pct = action.params.get("rollout_pct")
    if action.type == ActionType.TOGGLE_FEATURE_FLAG and action.params.get("value") and pct is not None and pct < 100:
        return [action, action.model_copy(update={"params": {**action.params, "rollout_pct": 100}})]
    return [action]


# --- the run ---------------------------------------------------------------------------


@dataclass
class Step:
    at: datetime
    kind: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"at": self.at.isoformat(), "kind": self.kind, "message": self.message, "data": self.data}


@dataclass
class RunResult:
    # verified, partially_verified, unverified, rolled_back, apply_failed, undo_failed
    status: str
    steps: list[Step]
    verdicts: list[tuple[str, Verdict]]
    preview: Optional[Preview] = None

    @property
    def final_verdict(self) -> Optional[Verdict]:
        return self.verdicts[-1][1] if self.verdicts else None

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "steps": [s.to_dict() for s in self.steps],
                "verdicts": [{"stage": n, **v.model_dump(mode="json")} for n, v in self.verdicts],
                "preview": self.preview.to_dict() if self.preview else None}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Runner:
    executor: Executor
    prometheus: PrometheusClient
    timing: Timing = field(default_factory=Timing)
    now: Callable[[], datetime] = _utcnow
    sleep: Callable[[float], None] = time.sleep
    on_step: Optional[Callable[[Step], None]] = None  # e.g. save progress so people can watch

    def run(self, action: ActionSpec, watch_service: str, limits: Optional[dict[str, float]] = None) -> RunResult:
        steps: list[Step] = []
        verdicts: list[tuple[str, Verdict]] = []

        def log(kind: str, message: str, **data: Any) -> None:
            step = Step(self.now(), kind, message, data)
            steps.append(step)
            if self.on_step:
                self.on_step(step)

        try:
            preview = self.executor.preview(action)
        except ExecutorError as e:
            log("apply_failed", f"Practice run failed: {e}")
            return RunResult("apply_failed", steps, verdicts)
        log("preview", "Practice run: " + "; ".join(preview.changes), **preview.to_dict())

        stages = rollout_stages(action)
        for i, stage in enumerate(stages, 1):
            final = i == len(stages)
            name = f"{stage.params.get('rollout_pct', 100):g}%" if len(stages) > 1 else "full"
            before = None if final else self._error_rate_before(watch_service)
            try:
                self.executor.apply(stage)
            except ExecutorError as e:
                log("apply_failed", f"Applying stage {name} failed: {e}")
                return self._undo(preview, "apply_failed", steps, verdicts, log)
            applied_at = self.now()
            log("applied", f"Applied stage {name}", stage=name, params=stage.params)

            window = self.timing.window_seconds if final else self.timing.stage_window_seconds
            criteria = recovery_criteria(window, limits) if final else stage_criteria(window, before)
            verdict = self._check(watch_service, criteria, applied_at, window)
            verdicts.append((name, verdict))
            log("checked", f"Check after stage {name}: {verdict.status.value.upper()}. {verdict.summary}",
                stage=name, status=verdict.status.value)

            if verdict.should_roll_back:
                return self._undo(preview, "rolled_back", steps, verdicts, log)
            if not final and verdict.status not in (VerificationStatus.VERIFIED,
                                                    VerificationStatus.PARTIALLY_VERIFIED):
                log("stopped", f"Not enough evidence that stage {name} is safe, so the fix was not widened. "
                               "A person should decide the next step.")
                return RunResult("unverified", steps, verdicts, preview)

        status = verdicts[-1][1].status.value
        return RunResult(status, steps, verdicts, preview)

    def _undo(self, preview: Preview, status: str, steps, verdicts, log) -> RunResult:
        try:
            self.executor.apply(preview.undo)
        except ExecutorError as e:
            log("undo_failed", f"Undo FAILED, a person must restore {preview.before} by hand: {e}")
            return RunResult("undo_failed", steps, verdicts, preview)
        log("undone", f"Undid the change: back to {preview.before}", before=preview.before)
        return RunResult(status, steps, verdicts, preview)

    def _error_rate_before(self, service: str) -> Optional[float]:
        end = self.now()
        try:
            obs = collect_observations(self.prometheus, service, ["http_5xx_rate"], end - timedelta(minutes=2), end)
        except PrometheusError:
            return None
        return max(o.value for o in obs) if obs else None  # the worst recent level

    def _check(self, service: str, criteria: list[SuccessCriterion], applied_at: datetime, window: int) -> Verdict:
        observe_from = applied_at + timedelta(seconds=self.timing.settle_seconds)
        done_at = observe_from + timedelta(seconds=window + SCRAPE_SLACK_SECONDS)
        wait = (done_at - self.now()).total_seconds()
        if wait > 0:
            self.sleep(wait)
        now = self.now()
        try:
            observations = collect_observations(
                self.prometheus, service, sorted({c.signal for c in criteria}),
                start=observe_from, end=now, baseline_at=applied_at)
        except PrometheusError:
            observations = []  # no data is never a success: the verdict will be UNVERIFIED
        return evaluate_recovery(criteria, observations, [], action_completed_at=observe_from, evaluated_at=now)
