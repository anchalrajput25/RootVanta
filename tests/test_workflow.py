"""The whole loop through the API and a worker: alert -> investigation -> agent's
proposal -> human approval -> practice run -> staged fix -> check -> verdict or undo."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import replay
from conftest import JWT_SECRET, auth
from fakes import Clock, FakeExecutor, FakeProm
from incident_agent.api.app import API_PREFIX, create_app
from incident_agent.api.config import Settings
from incident_agent.investigation.rules import Runbook
from incident_agent.remediation.runner import Runner, Timing
from incident_agent.workflow import queue
from incident_agent.workflow.worker import WorkerDeps, run_once

RUNBOOK = Runbook({"services": {"checkout": {"fallbacks": {"payments": {
    "flag": "use_backup_payment_provider", "description": "Switch checkout to the backup payment provider"}}}}})


class World:
    """The API, one worker, and fakes for everything outside."""

    def __init__(self, client: TestClient, shop: FakeExecutor):
        self.client, self.shop = client, shop
        self.prom = FakeProm()
        self.clock = Clock()
        self.sent: list[tuple[str, str]] = []
        collectors, meta = replay.load("payments_outage")
        app = client.app
        notifier = SimpleNamespace(send=lambda db, org_id, text, incident_id=None: self.sent.append((org_id, text)) or 1)
        self.deps = WorkerDeps(
            session_factory=app.state.session_factory,
            collectors=lambda org: collectors,
            prometheus=lambda org: self.prom,
            executor=lambda org: self.shop,
            runbook=RUNBOOK,
            timing=Timing(settle_seconds=60, window_seconds=120, stage_window_seconds=60),
            notifier=notifier,
            runner_factory=lambda **kw: Runner(**kw, now=self.clock.now, sleep=self.clock.sleep),
            clock=lambda: datetime.fromisoformat(meta["end"]),
        )

    def work(self) -> int:
        n = 0
        while run_once(self.deps, "test-worker"):
            n += 1
        return n


@pytest.fixture
def world(database_url):
    shop = FakeExecutor()
    shop.state["checkout"]["version"] = "v2.3.9"
    app = create_app(Settings(database_url=database_url, jwt_secret=JWT_SECRET),
                     connections=SimpleNamespace(executor=lambda org: shop, prometheus=lambda org: FakeProm()))
    with TestClient(app, base_url=f"http://test{API_PREFIX}") as c:
        yield World(c, shop)
    app.state.engine.dispose()


@pytest.fixture
def client(world):
    """The ``company`` fixture signs up through this client, so it must be the world's app."""
    return world.client


def open_incident(w: World, org: dict) -> str:
    r = w.client.post("/incidents", headers=auth(org["responder"]), json={
        "team_id": org["team_id"], "title": "checkout errors", "service": "checkout",
        "environment": "production", "severity": "sev2", "started_at": "2026-10-12T16:12:00Z"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def investigate_to_plan(w: World, org: dict) -> dict:
    incident_id = open_incident(w, org)
    r = w.client.post(f"/incidents/{incident_id}/investigations", headers=auth(org["responder"]), json={"minutes": 5})
    assert r.status_code == 202, r.text
    assert w.work() == 1
    inv = w.client.get(f"/investigations/{r.json()['id']}", headers=auth(org["responder"])).json()
    assert inv["status"] == "done" and inv["conclusion"] == "cause_identified"
    plan = w.client.get(f"/plans/{inv['plan_id']}", headers=auth(org["responder"])).json()
    return {"incident_id": incident_id, "investigation": inv, "plan": plan}


def approve(w: World, org: dict, plan_id: str) -> None:
    r = w.client.post(f"/plans/{plan_id}/approvals", headers=auth(org["approver"]), json={"decision": "approved"})
    assert r.status_code == 201, r.text


def test_full_loop_proves_the_fix_and_resolves_the_incident(world, company):
    org = company("Acme")
    found = investigate_to_plan(world, org)
    plan = found["plan"]
    assert "payments" in found["investigation"]["summary"]
    assert "## Proposed fix" in found["investigation"]["report_markdown"]
    # The agent proposed it, so a person must approve.
    assert plan["status"] == "awaiting_approval"
    assert plan["action"]["params"] == {"flag": "use_backup_payment_provider", "value": True, "rollout_pct": 10}
    assert any("waiting for approval" in text for _, text in world.sent)

    r = world.client.post(f"/plans/{plan['id']}/execute", headers=auth(org["responder"]))
    assert r.status_code == 409  # not approved yet

    approve(world, org, plan["id"])
    preview = world.client.post(f"/plans/{plan['id']}/dry-run", headers=auth(org["responder"])).json()
    assert preview["after"]["rollout_pct"] == 10 and preview["undo"]["params"]["value"] is False
    assert world.shop.applied == []  # practice run changed nothing

    r = world.client.post(f"/plans/{plan['id']}/execute", headers=auth(org["responder"]))
    assert r.status_code == 202, r.text
    assert world.client.post(f"/plans/{plan['id']}/execute", headers=auth(org["responder"])).status_code == 409
    assert world.work() == 1

    exe = world.client.get(f"/plans/{plan['id']}/execution", headers=auth(org["responder"])).json()
    assert exe["status"] == "verified"
    assert [s["kind"] for s in exe["steps"]] == ["preview", "applied", "checked", "applied", "checked"]
    assert world.shop.state["checkout"]["flags"]["use_backup_payment_provider"] is True
    assert world.client.get(f"/incidents/{found['incident_id']}", headers=auth(org["viewer"])).json()["status"] == "resolved"
    verifications = world.client.get(f"/plans/{plan['id']}/verifications", headers=auth(org["viewer"])).json()
    assert [v["verdict"]["stage"] for v in verifications] == ["10%", "100%"]
    assert any("Fixed and proven" in text for _, text in world.sent)


def test_failed_fix_is_undone_and_the_incident_stays_open(world, company):
    org = company("Acme")
    found = investigate_to_plan(world, org)
    plan_id = found["plan"]["id"]
    approve(world, org, plan_id)
    world.prom.new_error_from = world.clock.now() + timedelta(seconds=70)
    world.client.post(f"/plans/{plan_id}/execute", headers=auth(org["responder"]))
    world.work()

    exe = world.client.get(f"/plans/{plan_id}/execution", headers=auth(org["responder"])).json()
    assert exe["status"] == "rolled_back"
    assert world.shop.state["checkout"]["flags"]["use_backup_payment_provider"] is False
    assert world.client.get(f"/plans/{plan_id}", headers=auth(org["viewer"])).json()["status"] == "rolled_back"
    assert world.client.get(f"/incidents/{found['incident_id']}", headers=auth(org["viewer"])).json()["status"] == "open"
    assert any("undone automatically" in text for _, text in world.sent)


def test_a_fix_is_never_applied_twice_after_a_worker_dies(world, company):
    org = company("Acme")
    plan_id = investigate_to_plan(world, org)["plan"]["id"]
    approve(world, org, plan_id)
    world.client.post(f"/plans/{plan_id}/execute", headers=auth(org["responder"]))
    # Simulate a worker that marked the plan as executing and then died.
    with world.deps.session_factory() as db:
        from incident_agent.db.models import RecoveryPlanRow
        db.info["org_id"] = world.client.get("/auth/me", headers=auth(org["admin"])).json()["org_id"]
        db.get(RecoveryPlanRow, plan_id).status = "executing"
        db.commit()
    world.work()
    exe = world.client.get(f"/plans/{plan_id}/execution", headers=auth(org["responder"])).json()
    assert exe["status"] == "interrupted"
    assert world.shop.applied == []
    assert any("interrupted" in text for _, text in world.sent)


def test_only_one_worker_gets_each_job(world, company):
    org = company("Acme")
    incident_id = open_incident(world, org)
    world.client.post(f"/incidents/{incident_id}/investigations", headers=auth(org["responder"]))
    factory = world.deps.session_factory
    assert queue.claim(factory, "w1") is not None
    assert queue.claim(factory, "w2") is None


def test_viewers_cannot_start_work(world, company):
    org = company("Acme")
    incident_id = open_incident(world, org)
    assert world.client.post(f"/incidents/{incident_id}/investigations",
                             headers=auth(org["viewer"])).status_code == 403


def test_other_companies_see_nothing(world, company):
    a, b = company("Acme"), company("Globex")
    found = investigate_to_plan(world, a)
    inv_id, plan_id = found["investigation"]["id"], found["plan"]["id"]
    assert world.client.get(f"/investigations/{inv_id}", headers=auth(b["admin"])).status_code == 404
    assert world.client.get(f"/jobs/{found['investigation']['job_id']}", headers=auth(b["admin"])).status_code == 404
    assert world.client.post(f"/plans/{plan_id}/dry-run", headers=auth(b["admin"])).status_code == 404
    assert world.client.post(f"/plans/{plan_id}/execute", headers=auth(b["admin"])).status_code == 404


# --- alerts from monitoring ---------------------------------------------------------


def alert(service: str, status: str = "firing") -> dict:
    return {"status": status, "labels": {"alertname": "HighErrorRate", "service": service, "severity": "sev2"},
            "annotations": {"summary": f"{service} 5xx rate is 60%"}, "startsAt": "2026-10-12T16:14:56Z"}


def test_alert_opens_an_incident_and_starts_an_investigation(world, company):
    org = company("Acme")
    r = world.client.post("/alert-sources", headers=auth(org["admin"]),
                          json={"name": "prod alertmanager", "team_id": org["team_id"]})
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    hook = {"Authorization": f"Bearer {token}"}

    r = world.client.post("/alerts/alertmanager", headers=hook, json={"alerts": [alert("checkout")]})
    assert r.status_code == 200, r.text
    opened = r.json()["incidents_opened"]
    assert len(opened) == 1
    incident = world.client.get(f"/incidents/{opened[0]}", headers=auth(org["viewer"])).json()
    assert incident["title"] == "HighErrorRate: checkout 5xx rate is 60%" and incident["severity"] == "sev2"

    # The same problem alerting again is grouped, a resolved alert is ignored.
    r = world.client.post("/alerts/alertmanager", headers=hook,
                          json={"alerts": [alert("checkout"), alert("search", status="resolved")]})
    assert r.json() == {"incidents_opened": [], "grouped_into_existing": opened, "ignored": 1}

    assert world.work() == 1
    invs = world.client.get(f"/incidents/{opened[0]}/investigations", headers=auth(org["viewer"])).json()
    assert invs[0]["status"] == "done" and invs[0]["plan_id"]


def test_alerts_need_a_valid_token(world, company):
    company("Acme")
    r = world.client.post("/alerts/alertmanager", headers={"Authorization": "Bearer nope"},
                          json={"alerts": [alert("checkout")]})
    assert r.status_code == 401
    assert world.client.post("/alerts/alertmanager", json={"alerts": []}).status_code == 401


def test_only_admins_manage_alert_sources_and_channels(world, company):
    org = company("Acme")
    assert world.client.post("/alert-sources", headers=auth(org["responder"]),
                             json={"name": "x", "team_id": org["team_id"]}).status_code == 403
    r = world.client.post("/notification-targets", headers=auth(org["admin"]),
                          json={"url": "https://hooks.slack.com/services/T000/B000/abcdefghijklmnop"})
    assert r.status_code == 201
    assert "abcdefghijklmnop" not in r.json()["url_hint"]  # the secret part is hidden
    assert world.client.get("/notification-targets", headers=auth(org["responder"])).status_code == 403


def test_stats_count_proven_and_undone_fixes_per_company(world, company):
    org, other = company("Acme"), company("Globex")
    found = investigate_to_plan(world, org)
    approve(world, org, found["plan"]["id"])
    world.client.post(f"/plans/{found['plan']['id']}/execute", headers=auth(org["responder"]))
    world.work()
    open_incident(world, org)

    stats = world.client.get("/stats", headers=auth(org["viewer"]))
    assert stats.status_code == 200, stats.text
    body = stats.json()
    assert body["incidents_by_status"] == {"resolved": 1, "open": 1} and body["fixes_by_result"] == {"verified": 1}
    # Speed and cost of the one investigation (rules only, so no AI cost).
    assert body["investigations_done"] == 1 and body["median_investigation_seconds"] is not None
    assert body["ai_cost_usd_this_month"] == 0.0 and body["ai_investigations_this_month"] == 0
    # Another company sees only its own (empty) numbers.
    theirs = world.client.get("/stats", headers=auth(other["viewer"])).json()
    assert theirs["incidents_by_status"] == {} and theirs["fixes_by_result"] == {}
    assert theirs["investigations_done"] == 0 and theirs["median_investigation_seconds"] is None
    assert world.client.get("/stats").status_code == 401


def test_chat_messages_link_to_the_incident():
    from incident_agent.workflow.notify import Notifier

    posted = []
    http = httpx.Client(transport=httpx.MockTransport(lambda r: posted.append(json.loads(r.content)) or httpx.Response(200)))
    target = SimpleNamespace(id="ntf_1", url="https://hooks.slack.test/x")
    db = SimpleNamespace(scalars=lambda q: SimpleNamespace(all=lambda: [target]))
    assert Notifier(http=http, base_url="https://incidents.acme.com").send(db, "org_1", "Fixed.", "inc_9") == 1
    assert posted[0]["text"] == "Fixed.\n<https://incidents.acme.com/?incident=inc_9|Open in Rootvanta>"
    Notifier(http=http, base_url="").send(db, "org_1", "Fixed.", "inc_9")
    assert posted[1]["text"] == "Fixed."
