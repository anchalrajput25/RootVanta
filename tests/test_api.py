from __future__ import annotations

from conftest import PASSWORD, auth

INCIDENT = {
    "title": "Checkout 5xx spike after deploy",
    "service": "checkout",
    "environment": "production",
    "severity": "sev2",
    "started_at": "2026-01-01T11:50:00Z",
}
ROLLBACK = {
    "action": {
        "type": "rollback_deployment", "risk": "medium", "reversible": True,
        "blast_radius_pct": 25, "params": {"target_version": "v2.3.9"},
    },
    "expected_effect": "5xx rate returns below 1%",
}


def open_incident(client, org):
    r = client.post("/incidents", headers=auth(org["responder"]), json={**INCIDENT, "team_id": org["team_id"]})
    assert r.status_code == 201, r.text
    return r.json()


def propose(client, org, incident_id, body=ROLLBACK):
    r = client.post(f"/incidents/{incident_id}/plans", headers=auth(org["responder"]), json=body)
    assert r.status_code == 201, r.text
    return r.json()


def healthy_verification(window=600):
    t0 = "2026-01-01T12:00:00Z"
    samples = [
        {"signal": "http_5xx_rate", "at": f"2026-01-01T12:{m:02d}:00Z", "value": 0.002} for m in range(0, 11)
    ]
    return {
        "criteria": [{
            "id": "error_rate", "signal": "http_5xx_rate", "comparator": "lt", "threshold": 0.01,
            "window_seconds": window, "kind": "symptom",
        }],
        "observations": samples,
        "test_results": [{"id": "t1", "name": "checkout e2e", "passed": True}],
        "action_completed_at": t0,
        "evaluated_at": "2026-01-01T12:10:00Z",
    }


# --- auth -----------------------------------------------------------------------


def test_signup_login_and_me(client):
    r = client.post("/auth/signup", json={"organization_name": "Acme", "email": "Ops@Acme.com", "password": PASSWORD})
    assert r.status_code == 201
    r = client.post("/auth/login", json={"email": "ops@acme.com", "password": PASSWORD})
    assert r.status_code == 200
    me = client.get("/auth/me", headers=auth(r.json()["access_token"])).json()
    assert me["email"] == "ops@acme.com" and me["roles"] == ["admin"] and len(me["team_ids"]) == 1


def test_wrong_password_and_missing_token_are_rejected(client):
    client.post("/auth/signup", json={"organization_name": "Acme", "email": "a@acme.com", "password": PASSWORD})
    assert client.post("/auth/login", json={"email": "a@acme.com", "password": "wrong-password!"}).status_code == 401
    assert client.post("/auth/login", json={"email": "nobody@acme.com", "password": PASSWORD}).status_code == 401
    assert client.get("/incidents").status_code == 401
    assert client.get("/incidents", headers=auth("garbage")).status_code == 401


def test_duplicate_email_is_rejected(client):
    body = {"organization_name": "Acme", "email": "a@acme.com", "password": PASSWORD}
    assert client.post("/auth/signup", json=body).status_code == 201
    assert client.post("/auth/signup", json={**body, "organization_name": "Other"}).status_code == 409


def test_deactivated_user_loses_access_immediately(client, company):
    org = company("Acme")
    users = client.get("/users", headers=auth(org["admin"])).json()
    viewer_id = next(u["id"] for u in users if u["email"] == "viewer@acme.com")
    assert client.get("/incidents", headers=auth(org["viewer"])).status_code == 200
    client.post(f"/users/{viewer_id}/deactivate", headers=auth(org["admin"]))
    assert client.get("/incidents", headers=auth(org["viewer"])).status_code == 401


# --- roles ------------------------------------------------------------------------


def test_only_admins_manage_users_and_teams(client, company):
    org = company("Acme")
    new_user = {"email": "x@acme.com", "password": PASSWORD, "roles": ["admin"]}
    assert client.post("/users", headers=auth(org["responder"]), json=new_user).status_code == 403
    assert client.post("/teams", headers=auth(org["approver"]), json={"name": "sre"}).status_code == 403
    assert client.get("/audit", headers=auth(org["responder"])).status_code == 403
    assert client.post("/teams", headers=auth(org["admin"]), json={"name": "sre"}).status_code == 201


def test_viewer_can_read_but_not_act(client, company):
    org = company("Acme")
    incident = open_incident(client, org)
    assert client.get(f"/incidents/{incident['id']}", headers=auth(org["viewer"])).status_code == 200
    r = client.post("/incidents", headers=auth(org["viewer"]), json={**INCIDENT, "team_id": org["team_id"]})
    assert r.status_code == 403
    r = client.post(f"/incidents/{incident['id']}/plans", headers=auth(org["viewer"]), json=ROLLBACK)
    assert r.status_code == 201 and r.json()["status"] == "blocked"  # recorded, but policy refuses it


# --- company isolation ---------------------------------------------------------------


def test_companies_cannot_see_each_others_data(client, company):
    acme, globex = company("Acme"), company("Globex")
    incident = open_incident(client, acme)
    plan = propose(client, acme, incident["id"])

    assert client.get(f"/incidents/{incident['id']}", headers=auth(globex["admin"])).status_code == 404
    assert client.get(f"/plans/{plan['id']}", headers=auth(globex["admin"])).status_code == 404
    assert client.get("/incidents", headers=auth(globex["admin"])).json() == []
    emails = {u["email"] for u in client.get("/users", headers=auth(globex["admin"])).json()}
    assert not any(e.endswith("@acme.com") for e in emails)
    audit = client.get("/audit", headers=auth(globex["admin"])).json()
    assert all(e["object_id"] not in (incident["id"], plan["id"]) for e in audit)


def test_cannot_approve_or_create_in_another_company(client, company):
    acme, globex = company("Acme"), company("Globex")
    incident = open_incident(client, acme)
    plan = propose(client, acme, incident["id"])
    r = client.post(f"/plans/{plan['id']}/approvals", headers=auth(globex["approver"]), json={"decision": "approved"})
    assert r.status_code == 404
    r = client.post("/incidents", headers=auth(globex["admin"]), json={**INCIDENT, "team_id": acme["team_id"]})
    assert r.status_code == 404


# --- plans, approvals, verification ------------------------------------------------------


def test_destructive_action_is_blocked_and_cannot_be_approved(client, company):
    org = company("Acme")
    incident = open_incident(client, org)
    plan = propose(client, org, incident["id"], {
        "action": {"type": "delete_resource", "risk": "high", "reversible": False, "blast_radius_pct": 100},
        "expected_effect": "reset",
    })
    assert plan["status"] == "blocked"
    rules = {r["rule_id"] for r in plan["policy_decision"]["results"] if not r["passed"]}
    assert "action.allowlisted" in rules
    r = client.post(f"/plans/{plan['id']}/approvals", headers=auth(org["approver"]), json={"decision": "approved"})
    assert r.status_code == 409


def test_full_flow_propose_approve_verify(client, company):
    org = company("Acme")
    incident = open_incident(client, org)
    plan = propose(client, org, incident["id"])
    assert plan["status"] == "awaiting_approval"

    # Not verifiable before approval.
    r = client.post(f"/plans/{plan['id']}/verifications", headers=auth(org["responder"]), json=healthy_verification())
    assert r.status_code == 409

    r = client.post(f"/plans/{plan['id']}/approvals", headers=auth(org["approver"]),
                    json={"decision": "approved", "comment": "rollback target looks right"})
    assert r.status_code == 201
    assert client.get(f"/plans/{plan['id']}", headers=auth(org["viewer"])).json()["status"] == "ready"

    r = client.post(f"/plans/{plan['id']}/verifications", headers=auth(org["responder"]), json=healthy_verification())
    assert r.status_code == 201
    assert r.json()["status"] == "verified"
    assert client.get(f"/incidents/{incident['id']}", headers=auth(org["viewer"])).json()["status"] == "resolved"

    events = [e["event"] for e in client.get("/audit", headers=auth(org["admin"])).json()]
    for expected in ("incident.created", "plan.awaiting_approval", "plan.approved", "verification.verified"):
        assert expected in events


def test_missing_telemetry_is_unverified_and_incident_stays_open(client, company):
    org = company("Acme")
    incident = open_incident(client, org)
    plan = propose(client, org, incident["id"])
    client.post(f"/plans/{plan['id']}/approvals", headers=auth(org["approver"]), json={"decision": "approved"})
    body = {**healthy_verification(), "observations": [], "agent_claim": "Everything is fixed."}
    r = client.post(f"/plans/{plan['id']}/verifications", headers=auth(org["responder"]), json=body)
    assert r.json()["status"] == "unverified"
    assert client.get(f"/incidents/{incident['id']}", headers=auth(org["viewer"])).json()["status"] == "mitigating"


def test_failed_verification_recommends_rollback(client, company):
    org = company("Acme")
    incident = open_incident(client, org)
    plan = propose(client, org, incident["id"])
    client.post(f"/plans/{plan['id']}/approvals", headers=auth(org["approver"]), json={"decision": "approved"})
    body = healthy_verification()
    body["test_results"] = [{"id": "t2", "name": "coupon API", "passed": False}]
    r = client.post(f"/plans/{plan['id']}/verifications", headers=auth(org["responder"]), json=body)
    assert r.json()["status"] == "failed" and r.json()["verdict"]["should_roll_back"] is True


def test_requester_cannot_approve_own_plan(client, company):
    org = company("Acme")
    incident = open_incident(client, org)
    # The admin holds every role, so they pass the role check but not the four-eyes rule.
    r = client.post(f"/incidents/{incident['id']}/plans", headers=auth(org["admin"]), json=ROLLBACK)
    plan = r.json()
    r = client.post(f"/plans/{plan['id']}/approvals", headers=auth(org["admin"]), json={"decision": "approved"})
    assert r.status_code == 403


def test_responder_cannot_approve_and_double_decisions_are_rejected(client, company):
    org = company("Acme")
    incident = open_incident(client, org)
    plan = propose(client, org, incident["id"])
    r = client.post(f"/plans/{plan['id']}/approvals", headers=auth(org["responder"]), json={"decision": "approved"})
    assert r.status_code == 403
    first = client.post(f"/plans/{plan['id']}/approvals", headers=auth(org["approver"]), json={"decision": "rejected"})
    assert first.status_code == 201
    again = client.post(f"/plans/{plan['id']}/approvals", headers=auth(org["approver"]), json={"decision": "approved"})
    assert again.status_code == 409
    assert client.get(f"/plans/{plan['id']}", headers=auth(org["viewer"])).json()["status"] == "rejected"


def test_low_risk_dev_action_is_ready_without_approval(client, company):
    org = company("Acme")
    r = client.post("/incidents", headers=auth(org["responder"]),
                    json={**INCIDENT, "environment": "dev", "team_id": org["team_id"]})
    plan = propose(client, org, r.json()["id"], {
        "action": {"type": "restart_service", "risk": "low", "reversible": True, "blast_radius_pct": 10},
        "expected_effect": "pods restart cleanly",
    })
    assert plan["status"] == "ready"


def test_health(client):
    # healthz lives outside the API prefix
    assert client.get("http://test/healthz").json() == {"status": "ok"}


def test_a_sqlite_file_gives_each_request_its_own_connection(tmp_path):
    # Sharing one connection made parallel dashboard requests fail with random 401s.
    from incident_agent.db.session import make_engine

    engine = make_engine(f"sqlite:///{tmp_path / 'app.db'}")
    try:
        with engine.connect() as a, engine.connect() as b:
            assert a.connection.dbapi_connection is not b.connection.dbapi_connection
    finally:
        engine.dispose()
