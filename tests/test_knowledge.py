"""What the agent learns over time: the dependency map, alert grouping, and incident memory."""

# The World fixture (API plus one worker) is shared with test_workflow.
# ruff: noqa: F811 - pytest fixtures are passed as arguments with the fixture's own name

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from conftest import auth
from incident_agent.db.models import IncidentRow
from incident_agent.investigation.collectors import Collectors, EvidenceBook, _dependencies, service_calls
from incident_agent.investigation.models import ServiceCall
from incident_agent.knowledge import service_map
from incident_agent.telemetry.jaeger import JaegerError, Span
from test_workflow import alert, approve, client, investigate_to_plan, open_incident, world  # noqa: F401

T0 = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)


def span(trace: str, sid: str, parent: str | None, service: str, error: bool = False) -> Span:
    return Span(trace_id=trace, span_id=sid, parent_id=parent, service=service, operation="GET /",
                start=T0, duration_ms=5.0, error=error)


def shop_trace(n: int, payments_fails: bool = False) -> list[Span]:
    t = f"t{n}"
    return [span(t, "a", None, "frontend"), span(t, "b", "a", "checkout"), span(t, "c", "b", "checkout"),
            span(t, "d", "c", "payments", error=payments_fails), span(t, "e", "b", "inventory")]


class FakeJaeger:
    def __init__(self, traces: list[list[Span]], broken: frozenset[str] = frozenset()):
        self.data, self.broken = traces, broken

    def services(self) -> list[str]:
        return sorted({s.service for t in self.data for s in t} | set(self.broken))

    def traces(self, service, start, end, limit=50, errors_only=False):
        if service in self.broken:
            raise JaegerError("timeout")
        return [t for t in self.data if any(s.service == service for s in t)][:limit]


def org_id(w, org) -> str:
    return w.client.get("/auth/me", headers=auth(org["admin"])).json()["org_id"]


def save_map(w, org, *pairs: tuple[str, str]) -> None:
    with w.deps.session_factory() as db:
        db.info["org_id"] = org_id(w, org)
        service_map.save(db, db.info["org_id"], [ServiceCall(caller=a, callee=b, calls=10) for a, b in pairs])
        db.commit()


def alert_hook(w, org) -> dict:
    r = w.client.post("/alert-sources", headers=auth(org["admin"]), json={"name": "prod", "team_id": org["team_id"]})
    return {"Authorization": f"Bearer {r.json()['token']}"}


# --- dependency map -----------------------------------------------------------------


def test_calls_between_services_are_counted_once_per_call():
    calls = service_calls([shop_trace(1), shop_trace(2, payments_fails=True)])
    assert calls == [ServiceCall(caller="checkout", callee="inventory", calls=2),
                     ServiceCall(caller="checkout", callee="payments", calls=2, errors=1),
                     ServiceCall(caller="frontend", callee="checkout", calls=2)]


def test_saved_map_fills_in_when_the_window_has_no_traces():
    book = EvidenceBook()
    c = Collectors(jaeger=FakeJaeger([]), known_callees=lambda s: ["payments", "inventory"])
    assert _dependencies(c, book, "checkout", T0, T0 + timedelta(minutes=5)) == ["inventory", "payments"]
    assert book.items[0].source == "service-map" and book.items[0].facts["from_saved_map"] is True

    book = EvidenceBook()
    c = Collectors(jaeger=FakeJaeger([shop_trace(1)]), known_callees=lambda s: ["old-service"])
    assert _dependencies(c, book, "checkout", T0, T0) == ["inventory", "payments"]  # live traces win
    assert len(book.calls) == 3


def test_investigations_and_the_refresh_job_build_the_map(world, company):
    org, other = company("Acme"), company("Globex")
    investigate_to_plan(world, org)  # the recorded checkout -> payments outage
    shown = world.client.get("/service-map", headers=auth(org["viewer"])).json()
    assert ("checkout", "payments") in {(e["caller"], e["callee"]) for e in shown["edges"]}
    assert any(s["name"] == "checkout" and s["open_incident_id"] for s in shown["services"])

    world.deps.collectors = lambda org_: replace(Collectors(), jaeger=FakeJaeger([shop_trace(1)], frozenset({"ads"})))
    assert world.client.post("/service-map/refresh", headers=auth(org["viewer"])).status_code == 403
    r = world.client.post("/service-map/refresh", headers=auth(org["responder"]))
    assert r.status_code == 202, r.text
    assert world.client.post("/service-map/refresh", headers=auth(org["responder"])).status_code == 409
    assert world.work() == 1
    shown = world.client.get("/service-map", headers=auth(org["viewer"])).json()
    assert {(e["caller"], e["callee"]) for e in shown["edges"]} >= {("frontend", "checkout"),
                                                                    ("checkout", "inventory")}
    assert shown["last_refresh"]["status"] == "done"
    # Another company sees none of it.
    assert world.client.get("/service-map", headers=auth(other["viewer"])).json()["edges"] == []


def test_refresh_without_jaeger_says_what_to_connect(world, company):
    org = company("Acme")
    world.deps.collectors = lambda org_: Collectors()
    world.client.post("/service-map/refresh", headers=auth(org["responder"]))
    world.work()
    job = world.client.get("/service-map", headers=auth(org["viewer"])).json()["last_refresh"]
    assert job["status"] == "failed" and "Jaeger is not connected" in job["error"]


# --- alert grouping -------------------------------------------------------------------


def test_alerts_from_one_outage_become_one_incident(world, company):
    org = company("Acme")
    hook = alert_hook(world, org)
    save_map(world, org, ("frontend", "checkout"), ("checkout", "payments"))

    first = world.client.post("/alerts/alertmanager", headers=hook, json={"alerts": [alert("payments")]}).json()
    incident_id = first["incidents_opened"][0]
    worse = {**alert("checkout"), "labels": {**alert("checkout")["labels"], "severity": "critical"}}
    r = world.client.post("/alerts/alertmanager", headers=hook,
                          json={"alerts": [worse, alert("frontend"), alert("search")]}).json()
    # checkout calls payments, so it joins; frontend calls checkout, which is now in that incident, so it
    # joins too. search is not connected to any of them and gets its own incident.
    assert r["grouped_into_existing"] == [incident_id, incident_id]
    assert len(r["incidents_opened"]) == 1

    alerts = world.client.get(f"/incidents/{incident_id}/alerts", headers=auth(org["viewer"])).json()
    assert [(a["service"], a["reason"]) for a in alerts] == [("payments", "opened the incident"),
                                                            ("checkout", "checkout calls payments"),
                                                            ("frontend", "frontend calls checkout")]
    incident = world.client.get(f"/incidents/{incident_id}", headers=auth(org["viewer"])).json()
    assert incident["severity"] == "sev1"  # raised by the critical checkout alert


def test_a_dependency_alert_joins_the_callers_incident(world, company):
    org = company("Acme")
    hook = alert_hook(world, org)
    save_map(world, org, ("checkout", "payments"))
    incident_id = world.client.post("/alerts/alertmanager", headers=hook,
                                    json={"alerts": [alert("checkout")]}).json()["incidents_opened"][0]
    r = world.client.post("/alerts/alertmanager", headers=hook, json={"alerts": [alert("payments")]}).json()
    assert r["grouped_into_existing"] == [incident_id]
    alerts = world.client.get(f"/incidents/{incident_id}/alerts", headers=auth(org["viewer"])).json()
    assert alerts[-1]["reason"] == "checkout calls payments"


def test_old_incidents_do_not_swallow_new_neighbour_alerts(world, company):
    org = company("Acme")
    hook = alert_hook(world, org)
    save_map(world, org, ("checkout", "payments"))
    old = world.client.post("/alerts/alertmanager", headers=hook,
                            json={"alerts": [alert("payments")]}).json()["incidents_opened"][0]
    with world.deps.session_factory() as db:
        db.info["org_id"] = org_id(world, org)
        db.get(IncidentRow, old).created_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db.commit()
    r = world.client.post("/alerts/alertmanager", headers=hook, json={"alerts": [alert("checkout")]}).json()
    assert r["grouped_into_existing"] == [] and len(r["incidents_opened"]) == 1


# --- incident memory --------------------------------------------------------------------


def test_a_repeat_incident_remembers_the_last_cause_and_fix(world, company):
    org = company("Acme")
    first = investigate_to_plan(world, org)
    approve(world, org, first["plan"]["id"])
    world.client.post(f"/plans/{first['plan']['id']}/execute", headers=auth(org["responder"]))
    world.work()  # verified, so the first incident is resolved

    second = investigate_to_plan(world, org)
    notes = second["investigation"]["report"]["notes"]
    assert any(n.startswith("Seen before: 1 earlier incident with the same cause (failing dependency in payments)")
               and "worked and was proven" in n for n in notes)
    assert not any(n.startswith("Warning") for n in notes)
    assert any("Seen before" in text for _, text in world.sent)

    similar = world.client.get(f"/incidents/{second['incident_id']}/similar", headers=auth(org["viewer"])).json()
    assert [(s["incident_id"], s["match"], s["outcome"]) for s in similar] == [
        (first["incident_id"], "same cause", "verified")]
    assert "worked and was proven" in similar[0]["fix_text"]


def test_a_fix_that_failed_before_is_flagged(world, company):
    org = company("Acme")
    first = investigate_to_plan(world, org)
    approve(world, org, first["plan"]["id"])
    world.prom.new_error_from = world.clock.now() + timedelta(seconds=70)
    world.client.post(f"/plans/{first['plan']['id']}/execute", headers=auth(org["responder"]))
    world.work()  # rolled back
    world.prom.new_error_from = None

    second = investigate_to_plan(world, org)
    notes = second["investigation"]["report"]["notes"]
    assert any(n.startswith("Warning: the same fix (toggle feature flag on checkout) did not work") for n in notes)
    assert "Warning" in second["investigation"]["report_markdown"]


def test_memory_stays_inside_the_company(world, company):
    acme, globex = company("Acme"), company("Globex")
    investigate_to_plan(world, acme)
    theirs = investigate_to_plan(world, globex)
    assert not any("Seen before" in n for n in theirs["investigation"]["report"]["notes"])
    assert world.client.get(f"/incidents/{theirs['incident_id']}/similar",
                            headers=auth(globex["viewer"])).json() == []
    assert world.client.get(f"/incidents/{theirs['incident_id']}/similar",
                            headers=auth(acme["viewer"])).status_code == 404


# --- each service's own normal ----------------------------------------------------------


def _series(history: list[float], window: list[float]) -> tuple[list, datetime]:
    start = T0 + timedelta(seconds=15 * len(history))
    pts = [(T0 + timedelta(seconds=15 * i), v) for i, v in enumerate(history + window)]
    return pts, start


def test_a_small_rise_is_abnormal_for_a_service_that_is_usually_steady():
    from incident_agent.investigation.collectors import ANOMALY_RULES, _onset
    rule = ANOMALY_RULES["http_5xx_rate"]
    steady = [0.002 + 0.0002 * (i % 3) for i in range(40)]
    pts, start = _series(steady, [0.002] * 8 + [0.013] * 12)  # 1.3%: under the fixed 2% line
    i, _ = _onset(pts, start, rule)
    assert i is not None and pts[i][1] == 0.013


def test_the_same_rise_is_normal_for_a_service_that_is_usually_noisy():
    from incident_agent.investigation.collectors import ANOMALY_RULES, _onset
    rule = ANOMALY_RULES["http_5xx_rate"]
    noisy = [0.001, 0.008, 0.003, 0.012, 0.002, 0.010, 0.004, 0.009] * 5
    pts, start = _series(noisy, [0.004] * 8 + [0.013] * 12)
    assert _onset(pts, start, rule)[0] is None
