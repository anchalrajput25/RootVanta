"""A small stand-in for the Argo CD and Kubernetes APIs, served over real HTTP."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGO_TOKEN = "argo-token"
K8S_TOKEN = "k8s-token"
OLD, BAD = "a" * 40, "b" * 40


def argo_app(name: str, deployed_at: datetime, automated: bool = False) -> dict:
    return {
        "metadata": {"name": name},
        "spec": {"syncPolicy": {"automated": {"prune": True}} if automated else {}},
        "status": {
            "sync": {"status": "Synced", "revision": BAD},
            "health": {"status": "Degraded"},
            "history": [
                {"id": 1, "revision": OLD, "deployedAt": (deployed_at - timedelta(days=1)).isoformat()},
                {"id": 2, "revision": BAD, "deployedAt": deployed_at.isoformat()},
            ],
            "operationState": {"phase": "Succeeded"},
        },
    }


class Platform:
    def __init__(self, deployed_at: datetime | None = None):
        at = deployed_at or datetime.now(timezone.utc) - timedelta(minutes=5)
        self.apps = {"checkout": argo_app("checkout", at), "payments": argo_app("payments", at, automated=True)}
        self.scales = {("shop", "checkout"): 2}
        self.rollbacks: list[tuple[str, int]] = []
        self.fail_sync = False
        self.lock = threading.Lock()

    def rollback(self, name: str, history_id: int) -> tuple[int, dict]:
        app = self.apps[name]
        entry = next((h for h in app["status"]["history"] if h["id"] == history_id), None)
        if entry is None:
            return 400, {"message": "history id not found"}
        if app["spec"]["syncPolicy"].get("automated"):
            return 400, {"message": "rollback cannot be initiated when auto-sync is enabled"}
        self.rollbacks.append((name, history_id))
        if self.fail_sync:
            app["status"]["operationState"] = {"phase": "Failed", "message": "one or more objects failed to apply"}
            return 200, app
        history = app["status"]["history"]
        history.append({"id": history[-1]["id"] + 1, "revision": entry["revision"],
                        "deployedAt": datetime.now(timezone.utc).isoformat()})
        app["status"]["sync"]["revision"] = entry["revision"]
        app["status"]["operationState"] = {"phase": "Succeeded"}
        return 200, app


def serve(platform: Platform) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _route(self, method: str) -> None:
            token = self.headers.get("Authorization", "")
            path = self.path.split("?")[0]
            with platform.lock:
                if path.startswith("/api/v1/applications"):
                    if token != f"Bearer {ARGO_TOKEN}":
                        return self._send(401, {"message": "invalid session"})
                    if path == "/api/v1/applications" and method == "GET":
                        return self._send(200, {"items": [{"metadata": {"name": n}} for n in platform.apps]})
                    m = re.fullmatch(r"/api/v1/applications/([^/]+)(/rollback)?", path)
                    if not m or m.group(1) not in platform.apps:
                        return self._send(404, {"message": "not found"})
                    if m.group(2) and method == "POST":
                        return self._send(*platform.rollback(m.group(1), self._body()["id"]))
                    return self._send(200, platform.apps[m.group(1)])
                m = re.fullmatch(r"/apis/apps/v1/namespaces/([^/]+)/deployments/([^/]+)/scale", path)
                if m:
                    if token != f"Bearer {K8S_TOKEN}":
                        return self._send(401, {"message": "Unauthorized"})
                    key = (m.group(1), m.group(2))
                    if key not in platform.scales:
                        return self._send(404, {"message": "not found"})
                    if method == "PATCH":
                        platform.scales[key] = int(self._body()["spec"]["replicas"])
                    return self._send(200, {"kind": "Scale", "spec": {"replicas": platform.scales[key]}})
            self._send(404, {"message": "not found"})

        def do_GET(self):  # noqa: N802
            self._route("GET")

        def do_POST(self):  # noqa: N802
            self._route("POST")

        def do_PATCH(self):  # noqa: N802
            self._route("PATCH")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
