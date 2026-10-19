"""Reading verification signals from Prometheus (with a fake Prometheus)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from incident_agent.telemetry.check import check
from incident_agent.telemetry.prometheus import (
    NEW_ERROR_SIGNATURES,
    PrometheusClient,
    PrometheusError,
    collect_observations,
)
from incident_agent.verification.evaluator import VerificationStatus

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def series(metric: dict, values: list, step: int = 15, start: datetime = T0) -> dict:
    return {"metric": metric, "values": [[(start + timedelta(seconds=i * step)).timestamp(), str(v)]
                                          for i, v in enumerate(values)]}


def fake_prometheus(responses: dict[str, list[dict]], instant: dict[str, list[dict]] | None = None) -> PrometheusClient:
    """Answer each query with the result whose key appears in the PromQL."""
    instant = instant or {}

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        table = instant if request.url.path.endswith("/query") else responses
        result = next((r for key, r in table.items() if key in query), [])
        kind = "vector" if table is instant else "matrix"
        return httpx.Response(200, json={"status": "success", "data": {"resultType": kind, "result": result}})

    return PrometheusClient("http://prom", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_signal_values_become_observations_and_nan_is_dropped():
    prom = fake_prometheus({"service:http_5xx_rate": [series({"service": "checkout"}, [0.2, "NaN", 0.001])]})
    obs = collect_observations(prom, "checkout", ["http_5xx_rate"], T0, T0 + timedelta(minutes=1))
    assert [o.value for o in obs] == [0.2, 0.001]
    assert obs[0].at == T0 and obs[0].evidence_ref.startswith("prometheus:http_5xx_rate@")


def test_new_error_signatures_ignore_errors_known_before_the_fix():
    prom = fake_prometheus(
        {
            "rate(http_requests_total": [series({}, [5, 5, 5, 5])],
            "increase(app_errors_total": [
                series({"signature": "TypeError in pay_handler"}, [3, 1, 0, 0]),
                series({"signature": "AttributeError apply_coupon_v2"}, [0, 0, 2, 4]),
            ],
        },
        instant={"increase(app_errors_total": [{"metric": {"signature": "TypeError in pay_handler"}, "value": [0, "40"]}]},
    )
    obs = collect_observations(prom, "checkout", [NEW_ERROR_SIGNATURES], T0, T0 + timedelta(minutes=1))
    assert [o.value for o in obs] == [0, 0, 1, 1]


def test_no_traffic_data_means_no_error_observations_not_zero_errors():
    prom = fake_prometheus({})
    assert collect_observations(prom, "checkout", [NEW_ERROR_SIGNATURES], T0, T0 + timedelta(minutes=1)) == []


def test_unknown_signal_is_rejected():
    with pytest.raises(ValueError, match="made_up_signal"):
        collect_observations(fake_prometheus({}), "checkout", ["made_up_signal"], T0, T0)


def test_prometheus_errors_are_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "error", "error": "parse error"})

    prom = PrometheusClient("http://prom", http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(PrometheusError, match="parse error"):
        prom.range_query("up", T0, T0, 15)


def _healthy_checkout(start: datetime, minutes: int = 3) -> dict[str, list[dict]]:
    n = minutes * 4 + 1
    svc = {"service": "checkout"}
    return {
        "service:http_5xx_rate": [series(svc, [0.002] * n, start=start)],
        "service:p95_latency_ms": [series(svc, [120] * n, start=start)],
        "service:cpu_utilization": [series(svc, [0.4] * n, start=start)],
        "rate(http_requests_total": [series({}, [10] * n, start=start)],
    }


def test_live_check_verifies_a_healthy_fix():
    observe_from = T0 + timedelta(seconds=60)
    prom = fake_prometheus(_healthy_checkout(observe_from))
    v = check(prom, "bad_deploy_5xx", T0, now=observe_from + timedelta(seconds=150), window_seconds=120)
    assert v.status == VerificationStatus.VERIFIED, v.summary


def test_live_check_fails_when_errors_continue():
    observe_from = T0 + timedelta(seconds=60)
    data = _healthy_checkout(observe_from)
    data["service:http_5xx_rate"] = [series({"service": "checkout"}, [0.002, 0.2, 0.25], start=observe_from)]
    v = check(fake_prometheus(data), "bad_deploy_5xx", T0, now=observe_from + timedelta(seconds=150), window_seconds=120)
    assert v.status == VerificationStatus.FAILED and v.should_roll_back


def test_live_check_without_metrics_is_unverified():
    v = check(fake_prometheus({}), "bad_deploy_5xx", T0, now=T0 + timedelta(minutes=5), window_seconds=120)
    assert v.status == VerificationStatus.UNVERIFIED


def test_live_check_before_window_ends_is_unverified():
    observe_from = T0 + timedelta(seconds=60)
    prom = fake_prometheus(_healthy_checkout(observe_from, minutes=1))
    v = check(prom, "bad_deploy_5xx", T0, now=observe_from + timedelta(seconds=60), window_seconds=120)
    assert v.status == VerificationStatus.UNVERIFIED
