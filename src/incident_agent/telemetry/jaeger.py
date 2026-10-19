"""Read traces from Jaeger's query API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx


ERROR_SCAN_FACTOR = 10  # with errors_only, scan this many traces per one returned


class JaegerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Span:
    trace_id: str
    span_id: str
    parent_id: Optional[str]
    service: str
    operation: str
    start: datetime
    duration_ms: float
    error: bool


class JaegerClient:
    def __init__(self, base_url: str, http: Optional[httpx.Client] = None):
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=10.0)

    def services(self) -> list[str]:
        try:
            r = self.http.get(f"{self.base_url}/api/services")
            r.raise_for_status()
            names = r.json().get("data") or []
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            raise JaegerError(f"Jaeger query failed: {e}") from e
        return sorted(str(n) for n in names if n and not str(n).startswith("jaeger"))

    def traces(
        self, service: str, start: datetime, end: datetime, limit: int = 50, errors_only: bool = False
    ) -> list[list[Span]]:
        # Jaeger's tag search does not match the span status that OpenTelemetry
        # sets on errors, so failing traces are picked out here instead.
        params = {
            "service": service,
            "start": int(start.timestamp() * 1e6),
            "end": int(end.timestamp() * 1e6),
            "limit": limit * ERROR_SCAN_FACTOR if errors_only else limit,
        }
        try:
            r = self.http.get(f"{self.base_url}/api/traces", params=params)
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise JaegerError(f"Jaeger query failed: {e}") from e
        traces = [_spans(t) for t in body.get("data") or []]
        if errors_only:
            traces = [t for t in traces if any(s.error for s in t)][:limit]
        return traces


def _is_error(tags: list[dict]) -> bool:
    for t in tags:
        if t.get("key") == "error" and t.get("value") in (True, "true"):
            return True
        if t.get("key") == "otel.status_code" and t.get("value") == "ERROR":
            return True
    return False


def _spans(trace: dict) -> list[Span]:
    processes = trace.get("processes", {})
    out = []
    for s in trace.get("spans", []):
        parent = next((ref["spanID"] for ref in s.get("references", []) if ref.get("refType") == "CHILD_OF"), None)
        out.append(Span(
            trace_id=s["traceID"],
            span_id=s["spanID"],
            parent_id=parent,
            service=processes.get(s.get("processID"), {}).get("serviceName", "unknown"),
            operation=s.get("operationName", ""),
            start=datetime.fromtimestamp(s["startTime"] / 1e6, tz=timezone.utc),
            duration_ms=s.get("duration", 0) / 1000,
            error=_is_error(s.get("tags", [])),
        ))
    return out
