"""Fakes for step 5 tests: a clock, a shop, an executor and Prometheus."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from incident_agent.domain.models import ActionSpec, ActionType
from incident_agent.remediation.executor import ExecutorError, Preview
from incident_agent.telemetry.prometheus import SIGNAL_QUERIES

T0 = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


@dataclass
class Clock:
    t: datetime = T0

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


@dataclass
class FakeExecutor:
    """An in-memory shop: version, flags with rollout share, replicas."""

    state: dict[str, dict[str, Any]] = field(default_factory=lambda: {
        "checkout": {"version": "v2.4.0", "flags": {}, "flag_rollout": {}, "replicas": 4},
        "search": {"version": "v1.0.0", "flags": {}, "flag_rollout": {}, "replicas": 4},
    })
    applied: list[ActionSpec] = field(default_factory=list)
    fail_on_apply: int = 0  # fail the n-th apply (1-based); 0 = never

    def _get(self, service: str) -> dict[str, Any]:
        return self.state[service]

    def preview(self, action: ActionSpec) -> Preview:
        s, p = self._get(action.service), action.params
        if action.type == ActionType.ROLLBACK_DEPLOYMENT:
            before, after, undo = {"version": s["version"]}, {"version": p["target_version"]}, {
                "target_version": s["version"]}
        elif action.type == ActionType.TOGGLE_FEATURE_FLAG:
            v = s["flags"].get(p["flag"], False)
            pct = s["flag_rollout"].get(p["flag"], 100.0) if v else 0.0
            before = {"flag": p["flag"], "value": v, "rollout_pct": pct}
            after = {"flag": p["flag"], "value": p["value"], "rollout_pct": float(p.get("rollout_pct", 100))}
            undo = {"flag": p["flag"], "value": v, "rollout_pct": pct if v else 100.0}
        elif action.type == ActionType.SCALE_SERVICE:
            before, after, undo = {"replicas": s["replicas"]}, {"replicas": p["replicas"]}, {
                "replicas": s["replicas"]}
        else:
            raise ExecutorError("unsupported")
        return Preview(action, before, after, [f"{before} -> {after}"],
                       action.model_copy(update={"params": undo}), before == after)

    def apply(self, action: ActionSpec) -> dict[str, Any]:
        self.applied.append(action)
        if self.fail_on_apply == len(self.applied):
            raise ExecutorError("admin API timed out")
        s, p = self._get(action.service), action.params
        if action.type == ActionType.ROLLBACK_DEPLOYMENT:
            s["version"] = p["target_version"]
        elif action.type == ActionType.TOGGLE_FEATURE_FLAG:
            s["flags"][p["flag"]] = p["value"]
            s["flag_rollout"][p["flag"]] = float(p.get("rollout_pct", 100))
        elif action.type == ActionType.SCALE_SERVICE:
            s["replicas"] = p["replicas"]
        return s


Signal = Callable[[str, datetime], Optional[float]]


def healthy(signal: str, at: datetime) -> Optional[float]:
    return {"http_5xx_rate": 0.0, "p95_latency_ms": 120.0, "cpu_utilization": 0.5,
            "orders_per_minute": 300.0}.get(signal)


@dataclass
class FakeProm:
    """Answers the queries the verifier makes. ``values(signal, t)`` gives each
    sample (None = no data); ``new_error_from`` makes an unseen error
    signature appear from that time on."""

    values: Signal = healthy
    new_error_from: Optional[datetime] = None
    down: bool = False
    queries: list[str] = field(default_factory=list)

    def _check(self) -> None:
        if self.down:
            from incident_agent.telemetry.prometheus import PrometheusError
            raise PrometheusError("connection refused")

    def range_query(self, query: str, start: datetime, end: datetime, step_seconds: int):
        self._check()
        self.queries.append(query)
        times = []
        t = start
        while t <= end:
            times.append(t)
            t += timedelta(seconds=step_seconds)
        if query.startswith("sum by (signature)"):
            if self.new_error_from is None:
                return []
            return [({"signature": "AttributeError: apply_coupon_v2"},
                     [(t, 1.0 if t >= self.new_error_from else 0.0) for t in times])]
        if "http_requests_total" in query:
            return [({}, [(t, 1.0) for t in times])]
        for signal, template in SIGNAL_QUERIES.items():
            if query == template.format(service=_service(query)):
                points = [(t, v) for t in times if (v := self.values(signal, t)) is not None]
                return [({}, points)] if points else []
        return []

    def instant_query(self, query: str, at: datetime):
        self._check()
        return []  # no error signatures seen before the fix


def _service(query: str) -> str:
    return query.split('service="', 1)[1].split('"', 1)[0] if 'service="' in query else ""
