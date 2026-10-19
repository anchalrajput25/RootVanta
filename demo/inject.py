"""Break the practice shop on purpose, and apply the fixes.

    python inject.py status
    python inject.py bad-deploy                 # checkout v2.4.0: ~30% of orders fail
    python inject.py rollback                   # fix: back to v2.3.9
    python inject.py rollback --to v2.3.0       # "fix" that breaks the coupon API
    python inject.py payments-outage            # payment gateway returns 503
    python inject.py backup-provider            # fix: switch checkout to the backup provider
    python inject.py traffic-spike              # search gets 2x traffic and saturates
    python inject.py scale --replicas 8         # fix: more search replicas
    python inject.py reset                      # everything back to normal

Every fix records when it finished in .last_action.json, which the recovery
checker uses as the start of its observation window. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SERVICES = {"checkout": "http://localhost:8101", "payments": "http://localhost:8102", "search": "http://localhost:8103"}
TOKEN = "demo-admin-token"
LAST_ACTION = Path(__file__).with_name(".last_action.json")


def call(service: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        SERVICES[service] + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Admin-Token": TOKEN, "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)
    except urllib.error.URLError as e:
        sys.exit(f"could not reach {service} at {SERVICES[service]} ({e}). Is the demo running? "
                 "Start it with: docker compose up --build -d")


def fix_done(scenario: str, service: str, action: str, params: dict) -> None:
    at = datetime.now(timezone.utc).replace(microsecond=0)
    LAST_ACTION.write_text(json.dumps({"scenario": scenario, "service": service, "action": action,
                                       "params": params, "completed_at": at.isoformat()}, indent=2))
    print(f"fix applied at {at.isoformat()}")
    print(f"check it with:  python -m incident_agent.telemetry.check {scenario} --window 120 --wait")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("bad-deploy")
    rb = sub.add_parser("rollback")
    rb.add_argument("--to", default="v2.3.9")
    sub.add_parser("payments-outage")
    bp = sub.add_parser("backup-provider")
    bp.add_argument("--off", action="store_true")
    ts = sub.add_parser("traffic-spike")
    ts.add_argument("--multiplier", type=float, default=2.0)
    sc = sub.add_parser("scale")
    sc.add_argument("--replicas", type=int, required=True)
    sub.add_parser("reset")
    a = p.parse_args(argv)

    if a.cmd == "status":
        for name in SERVICES:
            print(name, json.dumps(call(name, "/admin/state")))
    elif a.cmd == "bad-deploy":
        call("checkout", "/admin/deploy", {"version": "v2.4.0"})
        print("checkout is now on v2.4.0; about 30% of orders will fail. Watch Grafana.")
    elif a.cmd == "rollback":
        call("checkout", "/admin/deploy", {"version": a.to})
        print(f"checkout rolled back to {a.to}")
        fix_done("bad_deploy_5xx", "checkout", "rollback_deployment", {"target_version": a.to})
    elif a.cmd == "payments-outage":
        call("payments", "/admin/faults", {"payments_outage": True})
        print("payment gateway now returns 503; checkout orders will fail.")
    elif a.cmd == "backup-provider":
        call("checkout", "/admin/flags", {"flag": "use_backup_payment_provider", "value": not a.off})
        print(f"use_backup_payment_provider = {not a.off}")
        if not a.off:
            fix_done("dependency_outage", "checkout", "toggle_feature_flag",
                     {"flag": "use_backup_payment_provider", "value": True})
    elif a.cmd == "traffic-spike":
        call("search", "/admin/faults", {"traffic_multiplier": a.multiplier})
        print(f"search traffic x{a.multiplier:g}; latency will climb.")
    elif a.cmd == "scale":
        call("search", "/admin/scale", {"replicas": a.replicas})
        print(f"search scaled to {a.replicas} replicas")
        fix_done("saturation_latency", "search", "scale_service", {"replicas": a.replicas})
    elif a.cmd == "reset":
        for name in SERVICES:
            call(name, "/admin/reset", {})
        LAST_ACTION.unlink(missing_ok=True)
        print("all services reset to normal")


if __name__ == "__main__":
    main()
