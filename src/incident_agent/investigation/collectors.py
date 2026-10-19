"""Gather evidence about one service from metrics, logs, traces and change events.

Collectors only report facts. They never decide what caused the incident;
that is the reasoning step's job. A telemetry system that cannot be reached
becomes a note, so the reasoning step knows what was not checked.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import fmean, median
from typing import Any, Callable, Optional

from incident_agent.domain.models import EvidenceKind
from incident_agent.investigation.models import Evidence, ServiceCall
from incident_agent.telemetry.jaeger import JaegerClient, JaegerError, Span
from incident_agent.telemetry.loki import LokiClient, LokiError
from incident_agent.telemetry.prometheus import SIGNAL_QUERIES, PrometheusClient, PrometheusError

STEP_SECONDS = 15
MAX_SIGNATURES_PER_SERVICE = 5
CHANGE_LOOKBACK = timedelta(minutes=15)
# Metrics before the window, used to learn what normal looks like.
HISTORY = timedelta(minutes=30)
DEPENDENCY_TRACES = 200  # recent traces scanned to find which services this one calls
# Untrusted text from logs is cut to this length before anything else sees it.
MAX_TEXT = 200


@dataclass(frozen=True)
class AnomalyRule:
    min_value: float  # below this a value is never abnormal
    ratio: float  # ...and it must also be this many times the baseline
    # A fast service can be badly slowed and still stay under min_value
    # (60 ms -> 200 ms). A rise of this many times the baseline, by at least
    # min_increase, is abnormal too.
    big_ratio: Optional[float] = None
    min_increase: float = 0.0
    # Compared with the service's own normal spread: a value this many robust standard
    # deviations above its usual level is abnormal, even under min_value, as long as it
    # is at least ``unusual_floor`` and rose by at least ``unusual_increase``.
    unusual_floor: float = 0.0
    unusual_increase: float = 0.0

    def abnormal(self, value: float, baseline: float, normal: Optional["Normal"] = None) -> bool:
        if value >= self.min_value and value >= baseline * self.ratio:
            return True
        if self.big_ratio and value >= baseline * self.big_ratio and value - baseline >= self.min_increase:
            return True
        return normal is not None and value >= self.unusual_floor and value - baseline >= self.unusual_increase \
            and normal.score(value) >= UNUSUAL_SCORE


@dataclass(frozen=True)
class Normal:
    """A service's usual level and spread for one signal, from the history before the window."""

    level: float  # median
    spread: float  # median absolute deviation, scaled to match a standard deviation

    def score(self, value: float) -> float:
        return (value - self.level) / self.spread if self.spread > 0 else 0.0

    @staticmethod
    def learn(history: list[float]) -> Optional["Normal"]:
        if len(history) < MIN_HISTORY:
            return None
        level = median(history)
        spread = 1.4826 * median(abs(v - level) for v in history)
        # Perfectly flat history (spread 0) says nothing about how much it usually moves.
        return Normal(level, spread) if spread > 0 else None


ANOMALY_RULES: dict[str, AnomalyRule] = {
    "http_5xx_rate": AnomalyRule(min_value=0.02, ratio=3.0, unusual_floor=0.01, unusual_increase=0.005),
    "p95_latency_ms": AnomalyRule(min_value=300, ratio=1.5, big_ratio=2.0, min_increase=100,
                                  unusual_floor=0, unusual_increase=50),
    "cpu_utilization": AnomalyRule(min_value=0.85, ratio=1.0, unusual_floor=0.7, unusual_increase=0.15),
}
# How unusual (in robust standard deviations of the service's own history) a value must be.
UNUSUAL_SCORE = 8.0
MIN_HISTORY = 20  # samples (5 minutes) of history needed to learn a service's normal spread
# A value must stay abnormal for this many samples in a row (30 s) to count:
# one noisy sample is a blip, not an incident.
PERSISTENCE = 2
UNITS = {"http_5xx_rate": "%", "p95_latency_ms": " ms", "cpu_utilization": "%"}


def _fmt(signal: str, v: float) -> str:
    return f"{v * 100:.1f}%" if UNITS[signal] == "%" else f"{v:.0f} ms"


def clean_text(text: Any) -> str:
    """Make untrusted log text safe to show and to pass to a model: one line, printable, short."""
    s = "".join(ch if ch.isprintable() else " " for ch in str(text))
    s = " ".join(s.split())
    return s if len(s) <= MAX_TEXT else s[: MAX_TEXT - 1] + "…"


@dataclass
class EvidenceBook:
    items: list[Evidence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    calls: list[ServiceCall] = field(default_factory=list)  # who calls whom in the traces that were read

    def add(self, **kwargs) -> Evidence:
        ev = Evidence(id=f"E{len(self.items) + 1}", **kwargs)
        self.items.append(ev)
        return ev


@dataclass
class Collectors:
    prometheus: Optional[PrometheusClient] = None
    loki: Optional[LokiClient] = None
    jaeger: Optional[JaegerClient] = None
    deploys: Optional[Any] = None  # e.g. ArgoCDDeploys: has deploys(start, end) -> list of Deploy
    # The services a service called in the past, from the company's saved dependency map.
    # Used when the window itself has no traces.
    known_callees: Optional[Callable[[str], list[str]]] = None


def collect(collectors: Collectors, service: str, start: datetime, end: datetime) -> EvidenceBook:
    book = EvidenceBook()
    dependencies = _dependencies(collectors, book, service, start, end)
    for svc in [service, *dependencies]:
        _metrics(collectors, book, svc, start, end)
    for svc in [service, *dependencies]:
        _error_logs(collectors, book, svc, start, end)
    _changes(collectors, book, start, end)
    _deploy_history(collectors, book, start, end)
    _error_traces(collectors, book, service, start, end)
    return book


# --- traces ----------------------------------------------------------------------


def _dependencies(c: Collectors, book: EvidenceBook, service: str, start: datetime, end: datetime) -> list[str]:
    if c.jaeger is None:
        book.notes.append("Traces not configured: dependencies and failing calls were not checked.")
        return []
    try:
        traces = c.jaeger.traces(service, start, end, limit=DEPENDENCY_TRACES)
    except JaegerError as e:
        book.notes.append(f"Jaeger unreachable ({e}): dependencies come from the saved map, if any.")
        traces = None
    if traces:
        book.calls = service_calls(traces)
        deps = sorted(set().union(*(_called_by(t, service) for t in traces)) - {service, "unknown"})
        book.add(
            kind=EvidenceKind.TRACE, source="jaeger", service=service, observed_at=end,
            locator=f"jaeger traces service={service} limit={DEPENDENCY_TRACES}",
            summary=f"{service} calls: {', '.join(deps)}" if deps else
                    f"{service} makes no traced calls to other services",
            facts={"dependencies": deps, "traces_sampled": len(traces)},
        )
        return deps
    known = _known_callees(c, book, service)
    if known:
        book.add(
            kind=EvidenceKind.TRACE, source="service-map", service=service, observed_at=end,
            locator=f"saved dependency map, callees of {service}",
            summary=f"No traces in the window; earlier traces show {service} calls: {', '.join(known)}",
            facts={"dependencies": known, "traces_sampled": 0, "from_saved_map": True},
        )
        return known
    if traces is not None:
        book.add(
            kind=EvidenceKind.TRACE, source="jaeger", service=service, observed_at=end,
            locator=f"jaeger traces service={service} limit={DEPENDENCY_TRACES}",
            summary=f"{service} makes no traced calls to other services",
            facts={"dependencies": [], "traces_sampled": 0},
        )
    return []


def _known_callees(c: Collectors, book: EvidenceBook, service: str) -> list[str]:
    if c.known_callees is None:
        return []
    try:
        return sorted(set(c.known_callees(service)) - {service, "unknown"})
    except Exception as e:  # noqa: BLE001 - the saved map is a hint; it must not stop the investigation
        book.notes.append(f"Saved dependency map unreadable ({clean_text(e)}).")
        return []


def service_calls(traces: list[list[Span]]) -> list[ServiceCall]:
    """Every caller -> callee pair across services in these traces, with how many calls failed."""
    calls: Counter[tuple[str, str]] = Counter()
    errors: Counter[tuple[str, str]] = Counter()
    for trace in traces:
        by_id = {s.span_id: s for s in trace}
        for s in trace:
            parent = by_id.get(s.parent_id) if s.parent_id else None
            if parent is None or parent.service == s.service or "unknown" in (parent.service, s.service):
                continue
            calls[(parent.service, s.service)] += 1
            errors[(parent.service, s.service)] += s.error
    return [ServiceCall(caller=a, callee=b, calls=n, errors=errors[(a, b)]) for (a, b), n in sorted(calls.items())]


def _called_by(trace: list[Span], service: str) -> set[str]:
    """Services reached from ``service``'s spans in one trace (callees, not callers)."""
    children: dict[str, list[Span]] = {}
    for s in trace:
        if s.parent_id:
            children.setdefault(s.parent_id, []).append(s)
    found: set[str] = set()
    stack = [s for s in trace if s.service == service]
    while stack:
        for child in children.get(stack.pop().span_id, []):
            if child.service != service:
                found.add(child.service)
            stack.append(child)
    return found


def _origin(trace: list[Span]) -> Optional[Span]:
    """The failing span furthest down the call chain: where the error started."""
    errors = [s for s in trace if s.error]
    if not errors:
        return None
    parent = {s.span_id: s.parent_id for s in trace}
    # Every failing span that is an ancestor of another failing span only
    # passed the error on.
    passed_on: set[str] = set()
    for s in errors:
        p = parent.get(s.span_id)
        while p:
            passed_on.add(p)
            p = parent.get(p)
    leaves = [s for s in errors if s.span_id not in passed_on]
    return min(leaves or errors, key=lambda s: s.start)


def _error_traces(c: Collectors, book: EvidenceBook, service: str, start: datetime, end: datetime) -> None:
    if c.jaeger is None:
        return
    try:
        traces = c.jaeger.traces(service, start, end, limit=50, errors_only=True)
    except JaegerError as e:
        book.notes.append(f"Jaeger unreachable ({e}): failing calls were not checked.")
        return
    origins = [o for o in (_origin(t) for t in traces) if o is not None]
    if not origins:
        book.add(
            kind=EvidenceKind.TRACE, source="jaeger", service=service, observed_at=end,
            locator=f"jaeger traces service={service} errors only",
            summary=f"No failing traces for {service} in the window",
            facts={"error_traces": 0, "origin_counts": {}},
        )
        return
    counts = Counter(o.service for o in origins)
    top, n = counts.most_common(1)[0]
    sample = next(o for o in origins if o.service == top)
    book.add(
        kind=EvidenceKind.TRACE, source="jaeger", service=service, observed_at=sample.start,
        locator=f"jaeger trace {sample.trace_id}",
        summary=f"In {n} of {len(origins)} failing {service} traces the error starts in {top} "
                f"({clean_text(sample.operation)})",
        facts={"error_traces": len(origins), "origin_counts": dict(counts), "top_origin": top,
               "sample_trace_id": sample.trace_id},
    )


# --- metrics ---------------------------------------------------------------------


def _onset(points: list[tuple[datetime, float]], start: datetime, rule: AnomalyRule) -> tuple[Optional[int], float]:
    """Index of the first abnormal point at or after ``start``, and the baseline used.

    The value must stay abnormal for PERSISTENCE samples in a row.
    "Normal" is the lower of two medians: the history before the window (at
    least 30 minutes), and the first third of the window. Either one may already be
    part of an incident (one that began early in the window, or an earlier
    incident just before it); the lower one is the healthier reference.
    """
    history = [v for t, v in points if t < start]
    window = [(i, v) for i, (t, v) in enumerate(points) if t >= start]
    if len(window) < 4:
        return None, 0.0
    candidates = [median(v for _, v in window[: max(1, len(window) // 3)])]
    if len(history) >= 4:
        candidates.append(median(history))
    baseline = min(candidates)
    normal = Normal.learn(history)
    flags = [rule.abnormal(v, baseline, normal) for _, v in window]
    for k, (i, _) in enumerate(window):
        run = flags[k:k + PERSISTENCE]
        if len(run) == PERSISTENCE and all(run):
            return i, baseline
    return None, baseline


def _metrics(c: Collectors, book: EvidenceBook, service: str, start: datetime, end: datetime) -> None:
    if c.prometheus is None:
        book.notes.append("Metrics not configured: error rate, latency and CPU were not checked.")
        return
    replicas: Optional[float] = None
    try:
        rep = c.prometheus.instant_query(f'app_replicas{{service="{service}"}}', end)
        replicas = rep[0][1] if rep else None
    except PrometheusError:
        pass
    for signal, rule in ANOMALY_RULES.items():
        query = SIGNAL_QUERIES[signal].format(service=service)
        try:
            result = c.prometheus.range_query(query, start - max(end - start, HISTORY), end, STEP_SECONDS)
        except PrometheusError as e:
            book.notes.append(f"Prometheus unreachable ({e}): metrics were not checked.")
            return
        points = result[0][1] if result else []
        in_window = [v for t, v in points if t >= start]
        if not in_window:
            continue
        i, baseline = _onset(points, start, rule)
        facts: dict[str, Any] = {"signal": signal, "anomalous": i is not None, "latest": points[-1][1],
                                 "peak": max(in_window), "baseline": baseline}
        if signal == "cpu_utilization" and replicas is not None:
            facts["replicas"] = replicas
        if i is None:
            summary = f"{service} {signal} normal (latest {_fmt(signal, points[-1][1])})"
            observed = points[-1][0]
        else:
            before = baseline
            after = fmean(v for _, v in points[i:])
            facts.update(before=before, after=after, onset=points[i][0].isoformat())
            summary = (f"{service} {signal} rose from {_fmt(signal, before)} to {_fmt(signal, after)} "
                       f"at {points[i][0]:%H:%M:%S}")
            observed = points[i][0]
        book.add(kind=EvidenceKind.METRIC, source="prometheus", service=service, observed_at=observed,
                 locator=query, summary=summary, facts=facts)


# --- logs ------------------------------------------------------------------------


def _error_logs(c: Collectors, book: EvidenceBook, service: str, start: datetime, end: datetime) -> None:
    if c.loki is None:
        book.notes.append("Logs not configured: error messages were not checked.")
        return
    query = f'{{service="{service}", level="ERROR"}}'
    try:
        lines = c.loki.query_range(query, start, end)
    except LokiError as e:
        book.notes.append(f"Loki unreachable ({e}): error logs were not checked.")
        return
    groups: dict[str, list] = {}
    for line in lines:
        data = line.json()
        sig = clean_text(data.get("error_signature") or data.get("msg") or "unknown error")
        groups.setdefault(sig, []).append((line.at, data))
    ranked = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)
    for sig, entries in ranked[:MAX_SIGNATURES_PER_SERVICE]:
        first_at, first = entries[0]
        book.add(
            kind=EvidenceKind.LOG, source="loki", service=service, observed_at=first_at,
            locator=f'{query} |= "{sig[:60]}"',
            summary=f"{len(entries)} '{sig}' errors in {service}, first at {first_at:%H:%M:%S}",
            facts={"signature": sig, "count": len(entries), "first_seen": first_at.isoformat(),
                   "last_seen": entries[-1][0].isoformat(), "version": clean_text(first.get("version", "")),
                   "sample_message": clean_text(first.get("msg", "")),
                   "sample_trace_id": clean_text(first.get("trace_id", ""))},
        )


def _changes(c: Collectors, book: EvidenceBook, start: datetime, end: datetime) -> None:
    if c.loki is None:
        return
    query = '{service=~".+"} |= "change: "'
    try:
        # A change made shortly before the window can still be the cause.
        lines = c.loki.query_range(query, start - CHANGE_LOOKBACK, end)
    except LokiError as e:
        book.notes.append(f"Loki unreachable ({e}): recent changes were not checked.")
        return
    for line in lines:
        data = line.json()
        kind = data.get("change_type")
        if kind not in ("deploy", "flag", "scale"):
            continue
        service = line.labels.get("service") or clean_text(data.get("service", "unknown"))
        if kind == "deploy":
            what = f"deployed {clean_text(data.get('to_version'))} (was {clean_text(data.get('from_version'))})"
        elif kind == "flag":
            pct = data.get("rollout_pct")
            share = f" for {pct:g}% of requests" if isinstance(pct, (int, float)) and pct < 100 else ""
            what = f"set flag {clean_text(data.get('flag'))} = {data.get('value')}{share}"
        else:
            what = f"scaled from {data.get('from_replicas')} to {data.get('to_replicas')} replicas"
        facts = {k: data[k] for k in ("from_version", "to_version", "flag", "value", "rollout_pct", "from_replicas", "to_replicas")
                 if k in data}
        book.add(
            kind=EvidenceKind.DEPLOYMENT if kind == "deploy" else EvidenceKind.INFRASTRUCTURE,
            source="loki", service=service, observed_at=line.at, locator=query,
            summary=f"{service}: {what} at {line.at:%H:%M:%S}",
            facts={"change_type": kind, **facts},
        )


def _deploy_history(c: Collectors, book: EvidenceBook, start: datetime, end: datetime) -> None:
    """Deploys recorded by the deploy system itself, so they are seen even when no log mentions them."""
    if c.deploys is None:
        return
    try:
        deploys = c.deploys.deploys(start - CHANGE_LOOKBACK, end)
    except Exception as e:  # noqa: BLE001 - a broken deploy system must not stop the investigation
        book.notes.append(f"Deploy history unreachable ({clean_text(e)}): deploys were not checked there.")
        return
    seen = {(e.service, e.facts.get("to_version")) for e in book.items if e.kind == EvidenceKind.DEPLOYMENT}
    for d in deploys:
        if (d.service, d.to_version) in seen:
            continue  # already known from the logs
        book.add(
            kind=EvidenceKind.DEPLOYMENT, source="argocd", service=d.service, observed_at=d.at,
            locator=f"argocd application history ({d.service})",
            summary=f"{d.service}: deployed {clean_text(d.to_version)[:12]} (was {clean_text(d.from_version)[:12]}) "
                    f"at {d.at:%H:%M:%S}",
            facts={"change_type": "deploy", "from_version": d.from_version, "to_version": d.to_version},
        )
