"""A stand-in for Prometheus, Loki and Jaeger that serves one practice incident.

It answers the same HTTP API calls the real systems do, with telemetry from
a generated practice incident (evals/practice.py) that is happening "now".
The load test points every test company's monitoring settings at it, so the
workers run real investigations without a real monitoring stack.

    python -m incident_agent.evals.fake_monitoring --port 9500
"""

from __future__ import annotations

import argparse
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from incident_agent.evals.practice import Case, generate

# A seed and case number that give a plain bad deploy with logs and traces.
SEED, FAULT = 1, "bad_deploy"


def incident_now(minutes_ago: int = 3) -> Case:
    """A bad-deploy incident whose generated data ends a few minutes from now."""
    end = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=minutes_ago)
    for i in range(200):
        case = generate(SEED, i, FAULT, end=end)
        if not case.distractions:
            return case
    raise RuntimeError("no plain practice case found")


def _ts(value: str) -> datetime:
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


class _Handler(BaseHTTPRequestHandler):
    case: Case  # set on the subclass made by serve()

    def log_message(self, *args) -> None:  # quiet
        pass

    def _send(self, body: Any, code: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        c = self.case.collectors
        if url.path == "/api/v1/query_range":
            result = c.prometheus.range_query(q["query"], _ts(q["start"]), _ts(q["end"]), int(float(q["step"])))
            self._send({"status": "success", "data": {"resultType": "matrix", "result": [
                {"metric": m, "values": [[t.timestamp(), str(v)] for t, v in pts]} for m, pts in result]}})
        elif url.path == "/api/v1/query":
            result = c.prometheus.instant_query(q["query"], _ts(q.get("time", str(datetime.now().timestamp()))))
            self._send({"status": "success", "data": {"resultType": "vector", "result": [
                {"metric": m, "value": [0, str(v)]} for m, v in result]}})
        elif url.path == "/loki/api/v1/query_range":
            lines = c.loki.query_range(q["query"], _ts(int(q["start"]) / 1e9), _ts(int(q["end"]) / 1e9),
                                       int(q.get("limit", 5000)))
            streams: dict[str, dict] = {}
            for line in lines:
                key = json.dumps(line.labels, sort_keys=True)
                streams.setdefault(key, {"stream": line.labels, "values": []})["values"].append(
                    [str(int(line.at.timestamp() * 1e9)), line.line])
            self._send({"status": "success", "data": {"resultType": "streams", "result": list(streams.values())}})
        elif url.path == "/loki/api/v1/labels":
            self._send({"status": "success", "data": ["service", "level"]})
        elif url.path == "/api/traces":
            traces = c.jaeger.traces(q["service"], _ts(int(q["start"]) / 1e6), _ts(int(q["end"]) / 1e6),
                                     int(q.get("limit", 50)))
            self._send({"data": [_jaeger(t) for t in traces]})
        elif url.path == "/api/services":
            self._send({"data": sorted({s.service for t in c.jaeger.data for s in t})})
        else:
            self._send({"status": "error", "error": f"not found: {url.path}"}, 404)


def _jaeger(trace) -> dict:
    services = sorted({s.service for s in trace})
    pid = {svc: f"p{i}" for i, svc in enumerate(services)}
    return {
        "traceID": trace[0].trace_id,
        "processes": {pid[svc]: {"serviceName": svc} for svc in services},
        "spans": [{
            "traceID": s.trace_id, "spanID": s.span_id, "operationName": s.operation, "processID": pid[s.service],
            "references": [{"refType": "CHILD_OF", "spanID": s.parent_id}] if s.parent_id else [],
            "startTime": int(s.start.timestamp() * 1e6), "duration": int(s.duration_ms * 1000),
            "tags": [{"key": "error", "value": True}] if s.error else [],
        } for s in trace],
    }


def serve(port: int = 0, case: Optional[Case] = None) -> tuple[ThreadingHTTPServer, Case]:
    """Start the server on a background thread. Port 0 picks a free port."""
    case = case or incident_now()
    handler = type("Handler", (_Handler,), {"case": case})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, case


def main(argv: Optional[list[str]] = None) -> None:
    p = argparse.ArgumentParser(description="Serve a practice incident as Prometheus, Loki and Jaeger")
    p.add_argument("--port", type=int, default=9500)
    args = p.parse_args(argv)
    server, case = serve(args.port)
    print(f"Serving a {case.fault} incident on http://127.0.0.1:{server.server_port} "
          f"(alerting service: {case.alerting}; expected cause: {case.expected.service}). Ctrl+C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
