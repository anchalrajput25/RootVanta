"""Read verification signals from Prometheus and turn them into Observations.

Signal names match the scenario files (evals/scenarios/*.yaml). Steps where
Prometheus has no data produce no observation, so a broken metrics pipeline
shows up as "not enough data" instead of looking healthy.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

import httpx

from incident_agent.verification.evaluator import Observation

# PromQL per signal; {service} is filled in. The recording rules live in
# demo/prometheus/rules.yml.
SIGNAL_QUERIES: dict[str, str] = {
    "http_5xx_rate": 'service:http_5xx_rate{{service="{service}"}}',
    "p95_latency_ms": 'service:p95_latency_ms{{service="{service}"}}',
    "orders_per_minute": 'service:orders_per_minute{{service="{service}"}}',
    "cpu_utilization": 'service:cpu_utilization{{service="{service}"}}',
}
NEW_ERROR_SIGNATURES = "new_error_signatures"
SUPPORTED_SIGNALS = frozenset(SIGNAL_QUERIES) | {NEW_ERROR_SIGNATURES}

# Error signatures seen in this long before the action count as "already
# known"; only signatures outside this set are new.
BASELINE_LOOKBACK = "30m"


class PrometheusError(RuntimeError):
    pass


Series = list[tuple[datetime, float]]


class PrometheusClient:
    def __init__(self, base_url: str, http: Optional[httpx.Client] = None):
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=10.0)

    def _get(self, path: str, params: dict) -> list[dict]:
        try:
            r = self.http.get(f"{self.base_url}{path}", params=params)
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise PrometheusError(f"Prometheus query failed: {e}") from e
        if body.get("status") != "success":
            raise PrometheusError(f"Prometheus error: {body.get('error', body)}")
        return body["data"]["result"]

    def range_query(self, query: str, start: datetime, end: datetime, step_seconds: int) -> list[tuple[dict, Series]]:
        result = self._get("/api/v1/query_range", {
            "query": query, "start": start.timestamp(), "end": end.timestamp(), "step": step_seconds,
        })
        return [(r["metric"], _points(r["values"])) for r in result]

    def instant_query(self, query: str, at: datetime) -> list[tuple[dict, float]]:
        result = self._get("/api/v1/query", {"query": query, "time": at.timestamp()})
        return [(r["metric"], float(r["value"][1])) for r in result]


def _points(values: list) -> Series:
    out = []
    for ts, raw in values:
        v = float(raw)
        if math.isfinite(v):  # NaN appears when there was no traffic to divide by
            out.append((datetime.fromtimestamp(float(ts), tz=timezone.utc), v))
    return out


def _evidence(signal: str, at: datetime) -> str:
    return f"prometheus:{signal}@{at.isoformat()}"


def _new_error_signatures(
    prom: PrometheusClient, service: str, start: datetime, end: datetime, step_seconds: int, baseline_at: datetime
) -> list[Observation]:
    errors = f'app_errors_total{{service="{service}"}}'
    baseline = {
        m.get("signature")
        for m, v in prom.instant_query(f"sum by (signature) (increase({errors}[{BASELINE_LOOKBACK}]))", baseline_at)
        if v > 0
    }
    # Steps where the service was being scraped at all. Without this, "no new
    # errors" could just mean "no data".
    alive = prom.range_query(f'sum(rate(http_requests_total{{service="{service}"}}[1m]))', start, end, step_seconds)
    steps = [at for _, series in alive for at, _ in series]

    new_at: dict[datetime, int] = {}
    for metric, series in prom.range_query(f"sum by (signature) (increase({errors}[1m]))", start, end, step_seconds):
        if metric.get("signature") in baseline:
            continue
        for at, v in series:
            if v > 0:
                new_at[at] = new_at.get(at, 0) + 1
    return [
        Observation(signal=NEW_ERROR_SIGNATURES, at=at, value=new_at.get(at, 0), evidence_ref=_evidence(NEW_ERROR_SIGNATURES, at))
        for at in sorted(steps)
    ]


def collect_observations(
    prom: PrometheusClient,
    service: str,
    signals: list[str],
    start: datetime,
    end: datetime,
    step_seconds: int = 15,
    baseline_at: Optional[datetime] = None,
) -> list[Observation]:
    """Fetch every requested signal for one service between start and end.

    ``baseline_at`` is when the fix finished: error signatures seen before it
    are known, any others are new. Defaults to ``start``."""
    unknown = set(signals) - SUPPORTED_SIGNALS
    if unknown:
        raise ValueError(f"no Prometheus query for signal(s): {', '.join(sorted(unknown))}")
    out: list[Observation] = []
    for signal in signals:
        if signal == NEW_ERROR_SIGNATURES:
            out += _new_error_signatures(prom, service, start, end, step_seconds, baseline_at or start)
            continue
        for _, series in prom.range_query(SIGNAL_QUERIES[signal].format(service=service), start, end, step_seconds):
            out += [Observation(signal=signal, at=at, value=v, evidence_ref=_evidence(signal, at)) for at, v in series]
    return out
