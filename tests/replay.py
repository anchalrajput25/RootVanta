"""Record real telemetry once, replay it in tests.

Recording wraps the real clients and stores every answer by query. Replaying
returns the same answers, so the tests run the full investigation on data
taken from the live practice shop, without any network.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from incident_agent.investigation.collectors import Collectors
from incident_agent.telemetry.jaeger import Span
from incident_agent.telemetry.loki import LogLine

FIXTURES = Path(__file__).parent / "fixtures" / "investigations"


def _key(*parts: Any) -> str:
    return "|".join(str(p) for p in parts)


class _Recorder:
    def __init__(self, store: dict, real: Any):
        self.store, self.real = store, real


class RecordingPrometheus(_Recorder):
    def range_query(self, query, start, end, step_seconds):
        result = self.real.range_query(query, start, end, step_seconds)
        self.store[_key("range", query)] = [[m, [[t.isoformat(), v] for t, v in pts]] for m, pts in result]
        return result

    def instant_query(self, query, at):
        result = self.real.instant_query(query, at)
        self.store[_key("instant", query)] = [[m, v] for m, v in result]
        return result


class RecordingLoki(_Recorder):
    def query_range(self, query, start, end, limit=5000):
        result = self.real.query_range(query, start, end, limit)
        self.store[_key("logs", query)] = [[l.at.isoformat(), l.labels, l.line] for l in result]
        return result


class RecordingJaeger(_Recorder):
    def traces(self, service, start, end, limit=50, errors_only=False):
        result = self.real.traces(service, start, end, limit, errors_only)
        self.store[_key("traces", service, errors_only)] = [
            [{**asdict(s), "start": s.start.isoformat()} for s in t] for t in result]
        return result


def recording(real: Collectors) -> tuple[Collectors, dict]:
    store: dict = {}
    return Collectors(RecordingPrometheus(store, real.prometheus), RecordingLoki(store, real.loki),
                      RecordingJaeger(store, real.jaeger)), store


class _Replay:
    def __init__(self, store: dict):
        self.store = store


class ReplayPrometheus(_Replay):
    def range_query(self, query, start, end, step_seconds):
        return [(m, [(datetime.fromisoformat(t), v) for t, v in pts])
                for m, pts in self.store.get(_key("range", query), [])]

    def instant_query(self, query, at):
        return [(m, v) for m, v in self.store.get(_key("instant", query), [])]


class ReplayLoki(_Replay):
    def query_range(self, query, start, end, limit=5000):
        return [LogLine(datetime.fromisoformat(at), labels, line)
                for at, labels, line in self.store.get(_key("logs", query), [])]


class ReplayJaeger(_Replay):
    def traces(self, service, start, end, limit=50, errors_only=False):
        return [[Span(**{**s, "start": datetime.fromisoformat(s["start"])}) for s in t]
                for t in self.store.get(_key("traces", service, errors_only), [])]


def load(name: str) -> tuple[Collectors, dict]:
    """Return (collectors that replay the fixture, the fixture's metadata)."""
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    store = data["store"]
    return Collectors(ReplayPrometheus(store), ReplayLoki(store), ReplayJaeger(store)), data["meta"]


def save(name: str, store: dict, meta: dict) -> Path:
    path = FIXTURES / f"{name}.json"
    path.write_text(json.dumps({"meta": meta, "store": store}, separators=(",", ":"), default=str), encoding="utf-8")
    return path
