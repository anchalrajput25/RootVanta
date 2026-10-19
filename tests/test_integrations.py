"""Settings: each company connects its own monitoring and change system, and manages its people."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from sqlalchemy import select

from conftest import PASSWORD, auth
from incident_agent.db.models import IntegrationRow
from incident_agent.integrations.crypto import SecretBox
from incident_agent.integrations.store import NotConnected, OrgConnections
from test_api import open_incident, propose

TOKEN = "prom-secret-token"


class FakeMonitoring(BaseHTTPRequestHandler):
    """Answers like Prometheus and a service admin API, and only with the right token."""

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/v1/query"):
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, {"status": "success", "data": {"result": [{}, {}]}})
        if self.path == "/admin/state" and self.headers.get("X-Admin-Token") == "admin-token":
            return self._send(200, {"version": "v1"})
        self._send(404, {})

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def monitoring():
    server = HTTPServer(("127.0.0.1", 0), FakeMonitoring)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def kinds(client, token):
    return {i["kind"]: i for i in client.get("/integrations", headers=auth(token)).json()}


def test_a_new_company_starts_with_nothing_connected(client, company):
    org = company("Acme")
    listed = kinds(client, org["admin"])
    assert set(listed) == {"prometheus", "loki", "jaeger", "service_admin", "argocd", "kubernetes"}
    assert not any(i["connected"] for i in listed.values())


def test_saved_token_is_encrypted_and_never_shown(client, company, monitoring):
    org = company("Acme")
    r = client.put("/integrations/prometheus", headers=auth(org["admin"]), json={"url": monitoring, "token": TOKEN})
    assert r.status_code == 200, r.text
    assert r.json()["has_token"] is True and TOKEN not in r.text
    assert TOKEN not in client.get("/integrations", headers=auth(org["admin"])).text
    assert TOKEN not in client.get("/audit", headers=auth(org["admin"])).text
    db = client.app.state.session_factory()
    db.info["org_id"] = client.get("/auth/me", headers=auth(org["admin"])).json()["org_id"]
    secret = db.scalar(select(IntegrationRow.secret))
    db.close()
    assert secret and TOKEN not in secret


def test_connection_test_says_whether_it_works(client, company, monitoring):
    org = company("Acme")
    h = auth(org["admin"])
    client.put("/integrations/prometheus", headers=h, json={"url": monitoring, "token": "wrong"})
    r = client.post("/integrations/prometheus/test", headers=h).json()
    assert r["last_test_ok"] is False and "refused" in r["last_test_message"]

    # Saving without a token keeps the saved one; here we replace it.
    client.put("/integrations/prometheus", headers=h, json={"url": monitoring, "token": TOKEN})
    r = client.post("/integrations/prometheus/test", headers=h).json()
    assert r["last_test_ok"] is True and "2 monitored targets" in r["last_test_message"]

    client.put("/integrations/prometheus", headers=h, json={"url": "http://127.0.0.1:1"})
    r = client.post("/integrations/prometheus/test", headers=h).json()
    assert r["last_test_ok"] is False and "Could not reach" in r["last_test_message"]
    assert r["has_token"] is True  # kept

    client.put("/integrations/service_admin", headers=h,
               json={"endpoints": {"checkout": monitoring}, "token": "admin-token"})
    r = client.post("/integrations/service_admin/test", headers=h).json()
    assert r["last_test_ok"] is True and "checkout" in r["last_test_message"]


def test_only_admins_change_connections_and_companies_stay_apart(client, company, monitoring):
    acme, globex = company("Acme"), company("Globex")
    assert client.put("/integrations/prometheus", headers=auth(acme["responder"]),
                      json={"url": monitoring}).status_code == 403
    assert client.get("/integrations", headers=auth(acme["viewer"])).status_code == 403
    client.put("/integrations/prometheus", headers=auth(acme["admin"]), json={"url": monitoring})
    assert kinds(client, acme["admin"])["prometheus"]["connected"] is True
    assert kinds(client, globex["admin"])["prometheus"]["connected"] is False


@pytest.mark.parametrize("url", ["ftp://example.com", "not a url", "http://169.254.169.254/latest"])
def test_unsafe_or_broken_addresses_are_refused(client, company, url):
    org = company("Acme")
    r = client.put("/integrations/prometheus", headers=auth(org["admin"]), json={"url": url})
    assert r.status_code == 422


def test_private_addresses_can_be_blocked_for_a_shared_service(client, company, monkeypatch):
    org = company("Acme")
    monkeypatch.setenv("INCIDENT_AGENT_BLOCK_PRIVATE_URLS", "1")
    r = client.put("/integrations/prometheus", headers=auth(org["admin"]), json={"url": "http://127.0.0.1:9090"})
    assert r.status_code == 422 and "private" in r.text


def test_workers_use_each_companys_own_connections(client, company, monitoring):
    acme, globex = company("Acme"), company("Globex")
    client.put("/integrations/prometheus", headers=auth(acme["admin"]), json={"url": monitoring, "token": TOKEN})
    me = {name: client.get("/auth/me", headers=auth(o["admin"])).json()["org_id"]
          for name, o in (("acme", acme), ("globex", globex))}
    conns = OrgConnections(client.app.state.session_factory, client.app.state.secret_box)
    prom = conns.prometheus(me["acme"])
    assert prom.http.headers["Authorization"] == f"Bearer {TOKEN}"
    collectors = conns.collectors(me["acme"])
    assert collectors.loki is None and collectors.jaeger is None  # optional
    with pytest.raises(NotConnected, match="Settings"):
        conns.prometheus(me["globex"])
    with pytest.raises(NotConnected):
        conns.executor(me["acme"])


def test_fixes_need_a_connected_change_system(client, company):
    org = company("Acme")
    plan = propose(client, org, open_incident(client, org)["id"])
    r = client.post(f"/plans/{plan['id']}/dry-run", headers=auth(org["responder"]))
    assert r.status_code == 409 and "change system" in r.json()["detail"]


def test_a_changed_secret_key_is_reported_not_crashed(client, company, monitoring):
    org = company("Acme")
    h = auth(org["admin"])
    client.put("/integrations/prometheus", headers=h, json={"url": monitoring, "token": TOKEN})
    client.app.state.connections.box = SecretBox("a-different-key")
    r = client.post("/integrations/prometheus/test", headers=h).json()
    assert r["last_test_ok"] is False and "save the token again" in r["last_test_message"]


# --- people -----------------------------------------------------------------------------


def test_admins_change_roles_but_never_lock_themselves_out(client, company):
    org = company("Acme")
    h = auth(org["admin"])
    users = {u["email"]: u for u in client.get("/users", headers=h).json()}
    viewer = users["viewer@acme.com"]
    r = client.put(f"/users/{viewer['id']}/roles", headers=h, json={"roles": ["responder"]})
    assert r.status_code == 200 and r.json()["roles"] == ["responder"]
    assert client.post("/incidents/x/investigations", headers=auth(org["viewer"]),
                       json={"minutes": 5}).status_code == 404  # now allowed to try (no such incident)
    admin = users["admin@acme.com"]
    assert client.put(f"/users/{admin['id']}/roles", headers=h, json={"roles": ["viewer"]}).status_code == 400
    assert client.put(f"/users/{viewer['id']}/roles", headers=auth(org["responder"]),
                      json={"roles": ["admin"]}).status_code == 403


def test_deactivated_people_can_be_let_back_in(client, company):
    org = company("Acme")
    h = auth(org["admin"])
    viewer = next(u for u in client.get("/users", headers=h).json() if u["email"] == "viewer@acme.com")
    client.post(f"/users/{viewer['id']}/deactivate", headers=h)
    assert client.post("/auth/login", json={"email": "viewer@acme.com", "password": PASSWORD}).status_code == 401
    assert client.post(f"/users/{viewer['id']}/activate", headers=h).json()["is_active"] is True
    assert client.post("/auth/login", json={"email": "viewer@acme.com", "password": PASSWORD}).status_code == 200


def test_a_revoked_alert_token_stops_working(client, company):
    org = company("Acme")
    h = auth(org["admin"])
    created = client.post("/alert-sources", headers=h, json={"name": "prod", "team_id": org["team_id"]}).json()
    hook = {"Authorization": f"Bearer {created['token']}"}
    assert client.post("/alerts/alertmanager", headers=hook, json={"alerts": []}).status_code == 200
    assert client.delete(f"/alert-sources/{created['id']}", headers=h).status_code == 204
    assert client.post("/alerts/alertmanager", headers=hook, json={"alerts": []}).status_code == 401
