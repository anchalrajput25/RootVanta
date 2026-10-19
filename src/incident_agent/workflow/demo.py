"""Walk through the whole loop on the practice shop, as the on-call people would.

    python -m incident_agent.workflow.demo setup    # demo company, users, and the Alertmanager token
    python -m incident_agent.workflow.demo watch    # follow incidents; approve fixes when asked

Needs the API (uvicorn) and a worker running. ``watch`` signs in as the
responder and the approver, prints each investigation, shows the practice run
of the proposed fix, and asks you to approve it. Then it follows the fix
stage by stage until the verdict.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from incident_agent.workflow.config import Connections

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TOKEN_FILE = PROJECT_ROOT / "demo" / "alertmanager" / "token"
COMPANY = "Demo Shop"
PASSWORD = "demo-password-123"  # practice environment only
USERS = {"admin": "admin@demo-shop.test", "responder": "oncall@demo-shop.test",
         "approver": "lead@demo-shop.test"}
FINAL = {"verified", "partially_verified", "unverified", "rolled_back", "apply_failed", "undo_failed",
         "interrupted"}


class Api:
    def __init__(self, base: str):
        self.http = httpx.Client(base_url=base.rstrip("/"), timeout=30.0)
        self.tokens: dict[str, str] = {}

    def call(self, method: str, path: str, as_: Optional[str] = None, **kw: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.tokens[as_]}"} if as_ else {}
        try:
            return self.http.request(method, path, headers=headers, **kw)
        except httpx.ConnectError:
            sys.exit(f"cannot reach the API at {self.http.base_url}. Start it with: "
                     "uvicorn incident_agent.api.app:app")

    def login(self, who: str) -> None:
        r = self.call("POST", "/auth/login", json={"email": USERS[who], "password": PASSWORD})
        if r.status_code != 200:
            sys.exit(f"cannot log in as {USERS[who]}; run the setup step first")
        self.tokens[who] = r.json()["access_token"]


def setup(api: Api) -> None:
    r = api.call("POST", "/auth/signup", json={"organization_name": COMPANY, "email": USERS["admin"],
                                               "password": PASSWORD})
    if r.status_code not in (201, 409):
        sys.exit(f"sign-up failed: {r.text}")
    api.login("admin")
    team_id = api.call("GET", "/teams", "admin").json()[0]["id"]
    for role in ("responder", "approver"):
        api.call("POST", "/users", "admin", json={"email": USERS[role], "password": PASSWORD, "roles": [role],
                                                  "team_ids": [team_id]})
    # Connect the company to the practice shop's monitoring, as a new company would in Settings.
    shop = Connections.from_env()
    for kind, body in (("prometheus", {"url": shop.prometheus_url}), ("loki", {"url": shop.loki_url}),
                       ("jaeger", {"url": shop.jaeger_url}),
                       ("service_admin", {"endpoints": shop.shop_endpoints, "token": shop.shop_token})):
        api.call("PUT", f"/integrations/{kind}", "admin", json=body).raise_for_status()
    r = api.call("POST", "/alert-sources", "admin", json={"name": "demo alertmanager", "team_id": team_id})
    r.raise_for_status()
    TOKEN_FILE.write_text(r.json()["token"], encoding="utf-8")
    print(f"Company '{COMPANY}' is ready.")
    for role, email in USERS.items():
        print(f"  {role:<10} {email}  password: {PASSWORD}")
    print(f"Alertmanager token saved to {TOKEN_FILE.relative_to(PROJECT_ROOT)}")
    print("Next: break something (python inject.py payments-outage) and run the 'watch' step.")


def _ask(question: str, yes: bool) -> bool:
    if yes:
        print(f"{question} y (auto)")
        return True
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def follow_execution(api: Api, plan_id: str) -> str:
    seen = 0
    while True:
        exe = api.call("GET", f"/plans/{plan_id}/execution", "responder").json()
        for step in exe["steps"][seen:]:
            print(f"  {step['at'][11:19]}  {step['message']}")
        seen = len(exe["steps"])
        if exe["status"] in FINAL:
            return exe["status"]
        time.sleep(5)


def handle(api: Api, incident: dict, yes: bool) -> Optional[str]:
    print(f"\n=== Incident: {incident['title']} ({incident['service']}, {incident['severity']}) ===")
    while True:
        invs = api.call("GET", f"/incidents/{incident['id']}/investigations", "responder").json()
        if invs and invs[0]["status"] in ("done", "failed"):
            break
        time.sleep(3)
    inv = invs[0]
    if inv["status"] == "failed":
        print("The investigation failed; see the worker's output.")
        return None
    print(inv["report_markdown"])
    if not inv["plan_id"]:
        print("No fix proposed for this incident.")
        return None
    plan = api.call("GET", f"/plans/{inv['plan_id']}", "responder").json()
    if plan["status"] != "awaiting_approval":
        print(f"The proposed fix is {plan['status']}.")
        return None
    preview = api.call("POST", f"/plans/{plan['id']}/dry-run", "responder")
    if preview.status_code == 200:
        print("Practice run (nothing changed yet):", "; ".join(preview.json()["changes"]))
    if not _ask(f"Approve as {USERS['approver']}?", yes):
        api.call("POST", f"/plans/{plan['id']}/approvals", "approver", json={"decision": "rejected"})
        print("Rejected. Nothing was changed.")
        return None
    api.call("POST", f"/plans/{plan['id']}/approvals", "approver", json={"decision": "approved"}).raise_for_status()
    api.call("POST", f"/plans/{plan['id']}/execute", "responder").raise_for_status()
    print("Approved. The worker is applying the fix and checking it:")
    status = follow_execution(api, plan["id"])
    print(f"Result: {status.upper()}")
    return status


def watch(api: Api, yes: bool, once: bool) -> None:
    api.login("responder")
    api.login("approver")
    done: set[str] = set()
    print("Waiting for incidents (alerts open them automatically)... Ctrl+C to stop.")
    while True:
        incidents = api.call("GET", "/incidents", "responder").json()
        for inc in sorted(incidents, key=lambda i: i["started_at"]):
            if inc["id"] in done or inc["status"] in ("resolved", "closed"):
                done.add(inc["id"])
                continue
            done.add(inc["id"])
            if handle(api, inc, yes) and once:
                return
        time.sleep(5)


def main(argv: Optional[list[str]] = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("step", choices=["setup", "watch"])
    p.add_argument("--api", default="http://localhost:8000/api/v1")
    p.add_argument("--yes", action="store_true", help="approve every proposed fix without asking")
    p.add_argument("--once", action="store_true", help="stop after the first fix has a result")
    a = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    api = Api(a.api)
    try:
        setup(api) if a.step == "setup" else watch(api, a.yes, a.once)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
