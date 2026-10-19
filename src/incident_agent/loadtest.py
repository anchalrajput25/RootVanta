"""Load test: many companies and many people using Rootvanta at once.

What it does, against a running API and workers:

1. Sets up N test companies, each with M people (admin, approvers,
   responders, viewers), an alert source, and monitoring settings that point
   at a built-in fake Prometheus/Loki/Jaeger serving a live practice incident.
2. For the chosen time, every person keeps using the dashboard (incident
   list, numbers, investigations), while each company's monitoring keeps
   sending alerts, which open incidents and queue investigations.
3. Follows every opened incident until its investigation finishes.
4. Checks that no company ever saw another company's incidents.

It prints response times per request type (p50/p95/p99), errors, requests
per second, how long an alert took to become a finished investigation, and
the result of the separation check.

    python -m incident_agent.loadtest --api http://localhost:8000 --companies 20 --people 10 --seconds 60

Run it against a test installation, not production: it creates companies and
incidents. Test companies are named "loadtest-<run id>-<n>".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import secrets
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from incident_agent.evals import fake_monitoring

PASSWORD = "loadtest-password-123"
ROLE_MIX = ["approver", "responder", "responder", "viewer"]  # after the first person, who is the admin


@dataclass
class Company:
    n: int
    team_id: str = ""
    alert_token: str = ""
    tokens: list[str] = field(default_factory=list)  # one per person
    incidents: set[str] = field(default_factory=set)  # every incident id this company opened
    alert_sent: dict[str, float] = field(default_factory=dict)  # incident id -> when its alert went out


@dataclass
class Stats:
    latency: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    status: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    errors: list[str] = field(default_factory=list)
    alert_to_done: list[float] = field(default_factory=list)
    conclusions: Counter = field(default_factory=Counter)
    isolation_checks: int = 0
    isolation_violations: list[str] = field(default_factory=list)

    def add(self, name: str, seconds: float, code: int) -> None:
        self.latency[name].append(seconds)
        self.status[name][code] += 1


async def _sleep_until(seconds: float, until: float) -> None:
    """Sleep, but never past the end of the test."""
    await asyncio.sleep(max(0.0, min(seconds, until - time.monotonic())))


def _pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(p * (len(s) - 1) + 0.5))] if s else 0.0


class Runner:
    def __init__(self, api: str, monitoring_url: str, alerting_service: str, services: list[str],
                 companies: int, people: int, seconds: float, alert_every: float, think: float):
        self.api = api.rstrip("/") + "/api/v1"
        self.monitoring_url = monitoring_url
        self.alerting_service = alerting_service
        self.services = services
        self.companies = [Company(i) for i in range(companies)]
        self.people, self.seconds, self.alert_every, self.think = people, seconds, alert_every, think
        self.run_id = secrets.token_hex(3)
        self.stats = Stats()
        self.client = httpx.AsyncClient(timeout=30.0, limits=httpx.Limits(max_connections=200,
                                                                          max_keepalive_connections=200))

    async def call(self, name: str, method: str, path: str, token: Optional[str] = None, **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        t = time.perf_counter()
        try:
            r = await self.client.request(method, self.api + path, headers=headers, **kw)
        except httpx.HTTPError as e:
            self.stats.add(name, time.perf_counter() - t, 0)
            self.stats.errors.append(f"{name}: {type(e).__name__}")
            raise
        self.stats.add(name, time.perf_counter() - t, r.status_code)
        if r.status_code >= 500:
            self.stats.errors.append(f"{name}: HTTP {r.status_code}")
        return r

    # --- 1. setup -------------------------------------------------------------------

    async def setup_company(self, c: Company) -> None:
        admin_email = f"admin-{self.run_id}-{c.n}@loadtest.example"
        r = await self.call("signup", "POST", "/auth/signup", json={
            "organization_name": f"loadtest-{self.run_id}-{c.n}", "email": admin_email, "password": PASSWORD})
        r.raise_for_status()
        admin = r.json()["access_token"]
        c.tokens.append(admin)
        c.team_id = (await self.call("teams", "GET", "/teams", admin)).json()[0]["id"]
        for kind in ("prometheus", "loki", "jaeger"):
            r = await self.call("save monitoring", "PUT", f"/integrations/{kind}", admin,
                                json={"url": self.monitoring_url})
            r.raise_for_status()
        r = await self.call("alert source", "POST", "/alert-sources", admin,
                            json={"name": "load test", "team_id": c.team_id})
        r.raise_for_status()
        c.alert_token = r.json()["token"]
        for p in range(1, self.people):
            email = f"p{p}-{self.run_id}-{c.n}@loadtest.example"
            r = await self.call("add person", "POST", "/users", admin, json={
                "email": email, "password": PASSWORD, "roles": [ROLE_MIX[(p - 1) % len(ROLE_MIX)]],
                "team_ids": [c.team_id]})
            r.raise_for_status()
            r = await self.call("login", "POST", "/auth/login", json={"email": email, "password": PASSWORD})
            r.raise_for_status()
            c.tokens.append(r.json()["access_token"])

    # --- 2. steady load -------------------------------------------------------------

    async def person(self, c: Company, token: str, until: float) -> None:
        rng = random.Random()
        await asyncio.sleep(rng.uniform(0, self.think))
        while time.monotonic() < until:
            pick = rng.random()
            try:
                if pick < 0.40 or not c.incidents:
                    await self.call("list incidents", "GET", "/incidents", token, params={"limit": 50})
                elif pick < 0.60:
                    await self.call("numbers (stats)", "GET", "/stats", token)
                elif pick < 0.85:
                    await self.call("investigations", "GET",
                                    f"/incidents/{rng.choice(sorted(c.incidents))}/investigations", token)
                elif pick < 0.95:
                    await self.call("one incident", "GET", f"/incidents/{rng.choice(sorted(c.incidents))}", token)
                else:
                    await self.call("who am I", "GET", "/auth/me", token)
            except httpx.HTTPError:
                pass
            await _sleep_until(rng.uniform(0.5, 1.5) * self.think, until)

    async def alerts(self, c: Company, until: float) -> None:
        rng = random.Random()
        await asyncio.sleep(rng.uniform(0, self.alert_every))
        while time.monotonic() < until:
            # Mostly the service that is really broken; sometimes others, which are healthy or unknown.
            service = self.alerting_service if rng.random() < 0.5 else rng.choice(self.services)
            body = {"alerts": [{"status": "firing", "labels": {"alertname": "HighErrorRate", "service": service,
                                                               "severity": "critical"},
                                "annotations": {"summary": f"{service} errors"},
                                "startsAt": datetime.now(timezone.utc).isoformat()}]}
            try:
                sent = time.perf_counter()
                r = await self.call("alert in", "POST", "/alerts/alertmanager", c.alert_token, json=body)
                if r.status_code == 200:
                    for inc in r.json()["incidents_opened"]:
                        c.incidents.add(inc)
                        c.alert_sent[inc] = sent
            except httpx.HTTPError:
                pass
            await _sleep_until(rng.uniform(0.5, 1.5) * self.alert_every, until)

    # --- 3. wait for the investigations ---------------------------------------------

    async def track_investigations(self, load_done: asyncio.Event, timeout: float) -> int:
        """Poll every opened incident once a second, as a dashboard would, until its investigation finishes.

        Runs during the load, so the time from alert to finished investigation is
        measured as it happens. Returns how many were still unfinished at the end.
        """
        done: set[str] = set()
        deadline: Optional[float] = None

        async def check(c: Company, inc: str) -> None:
            try:
                r = await self.call("investigations", "GET", f"/incidents/{inc}/investigations", c.tokens[0])
            except httpx.HTTPError:
                return
            runs = r.json() if r.status_code == 200 else []
            if runs and runs[0]["status"] in ("done", "failed") and inc not in done:
                done.add(inc)
                self.stats.alert_to_done.append(time.perf_counter() - c.alert_sent[inc])
                self.stats.conclusions[runs[0]["conclusion"] or runs[0]["status"]] += 1

        while True:
            pending = [(c, inc) for c in self.companies for inc in list(c.alert_sent) if inc not in done]
            if load_done.is_set():
                deadline = deadline or time.monotonic() + timeout
                if not pending or time.monotonic() > deadline:
                    return len(pending)
            await asyncio.gather(*(check(c, inc) for c, inc in pending))
            await asyncio.sleep(1.0)

    # --- 4. separation check --------------------------------------------------------

    async def check_separation(self) -> None:
        for c in self.companies:
            r = await self.call("list incidents", "GET", "/incidents", c.tokens[-1], params={"limit": 500})
            seen = {i["id"] for i in r.json()}
            self.stats.isolation_checks += 1
            if seen - c.incidents:
                self.stats.isolation_violations.append(f"company {c.n} saw {len(seen - c.incidents)} foreign incidents")
            other = self.companies[(c.n + 1) % len(self.companies)]
            for inc in list(other.incidents)[:3]:
                if other is c:
                    break
                r = await self.call("foreign incident", "GET", f"/incidents/{inc}", c.tokens[-1])
                self.stats.isolation_checks += 1
                if r.status_code != 404:
                    self.stats.isolation_violations.append(f"company {c.n} got HTTP {r.status_code} for another "
                                                           f"company's incident")

    async def run(self, drain_timeout: float) -> dict[str, Any]:
        t = time.perf_counter()
        sem = asyncio.Semaphore(10)

        async def one(c: Company) -> None:
            async with sem:
                await self.setup_company(c)
        await asyncio.gather(*(one(c) for c in self.companies))
        setup_seconds = time.perf_counter() - t
        print(f"Set up {len(self.companies)} companies with {self.people} people each in {setup_seconds:.0f} s",
              file=sys.stderr)

        steady_from = {k: len(v) for k, v in self.stats.latency.items()}
        until = time.monotonic() + self.seconds
        load_done = asyncio.Event()
        tracker = asyncio.create_task(self.track_investigations(load_done, drain_timeout))
        t = time.perf_counter()
        await asyncio.gather(*(self.person(c, tok, until) for c in self.companies for tok in c.tokens),
                             *(self.alerts(c, until) for c in self.companies))
        steady_seconds = time.perf_counter() - t
        steady_requests = sum(len(v) - steady_from.get(k, 0) for k, v in self.stats.latency.items())
        print(f"Steady load done: {steady_requests} requests in {steady_seconds:.0f} s; waiting for investigations",
              file=sys.stderr)
        load_done.set()
        unfinished = await tracker
        await self.check_separation()
        await self.client.aclose()
        return self.report(setup_seconds, steady_seconds, steady_requests, unfinished)

    def report(self, setup_seconds: float, steady_seconds: float, steady_requests: int, unfinished: int) -> dict:
        s = self.stats
        endpoints = {}
        for name, values in sorted(s.latency.items()):
            codes = s.status[name]
            endpoints[name] = {"requests": len(values), "p50_ms": round(_pct(values, 0.50) * 1000, 1),
                               "p95_ms": round(_pct(values, 0.95) * 1000, 1),
                               "p99_ms": round(_pct(values, 0.99) * 1000, 1),
                               "server_errors": sum(n for code, n in codes.items() if code >= 500 or code == 0),
                               "statuses": dict(codes)}
        total = sum(len(v) for v in s.latency.values())
        opened = sum(len(c.alert_sent) for c in self.companies)
        return {
            "companies": len(self.companies), "people_per_company": self.people,
            "people_total": len(self.companies) * self.people,
            "setup_seconds": round(setup_seconds, 1), "steady_seconds": round(steady_seconds, 1),
            "requests_total": total, "steady_requests_per_second": round(steady_requests / steady_seconds, 1),
            "server_errors": sum(e["server_errors"] for e in endpoints.values()),
            "incidents_opened": opened, "investigations_finished": len(s.alert_to_done),
            "investigations_unfinished": unfinished,
            "alert_to_finished_p50_s": round(statistics.median(s.alert_to_done), 1) if s.alert_to_done else None,
            "alert_to_finished_p95_s": round(_pct(s.alert_to_done, 0.95), 1) if s.alert_to_done else None,
            "conclusions": dict(s.conclusions),
            "separation_checks": s.isolation_checks, "separation_violations": s.isolation_violations,
            "endpoints": endpoints,
            "sample_errors": s.errors[:10],
        }


def format_report(r: dict) -> str:
    lines = [
        f"{r['companies']} companies x {r['people_per_company']} people = {r['people_total']} people at once, "
        f"for {r['steady_seconds']:.0f} s",
        f"  Requests: {r['requests_total']} total, {r['steady_requests_per_second']} per second while everyone "
        f"was active; server errors: {r['server_errors']}",
        f"  Alerts opened {r['incidents_opened']} incidents; investigations finished: "
        f"{r['investigations_finished']}, unfinished: {r['investigations_unfinished']}",
        f"  Alert to finished investigation: p50 {r['alert_to_finished_p50_s']} s, p95 {r['alert_to_finished_p95_s']} s",
        f"  Conclusions: {r['conclusions']}",
        f"  Separation between companies: {r['separation_checks']} checks, "
        f"{len(r['separation_violations'])} problems",
        "",
        f"  {'request':<20}{'count':>8}{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}{'5xx':>6}",
    ]
    for name, e in r["endpoints"].items():
        lines.append(f"  {name:<20}{e['requests']:>8}{e['p50_ms']:>9}{e['p95_ms']:>9}{e['p99_ms']:>9}"
                     f"{e['server_errors']:>6}")
    if r["sample_errors"]:
        lines += ["", "  Sample errors: " + "; ".join(r["sample_errors"])]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api", default="http://localhost:8000", help="where the API runs")
    p.add_argument("--companies", type=int, default=20)
    p.add_argument("--people", type=int, default=10, help="people per company (the first is the admin)")
    p.add_argument("--seconds", type=float, default=60, help="how long everyone keeps working")
    p.add_argument("--alert-every", type=float, default=10, help="seconds between alerts, per company")
    p.add_argument("--think", type=float, default=2, help="seconds a person waits between clicks, on average")
    p.add_argument("--drain-timeout", type=float, default=300, help="how long to wait for investigations")
    p.add_argument("--monitoring-port", type=int, default=9500, help="port for the built-in fake monitoring")
    p.add_argument("--monitoring-host", default="127.0.0.1",
                   help="host name the workers use to reach the fake monitoring (e.g. host.docker.internal)")
    p.add_argument("--json", type=Path, help="also write the results to this file")
    args = p.parse_args(argv)

    server, case = fake_monitoring.serve(args.monitoring_port)
    # The incident's own services (one broken, the rest healthy), plus some the monitoring knows nothing about.
    services = sorted({q.split('"')[1] for q in case.collectors.prometheus.by_query})
    services += [f"unknown-{i}" for i in range(10)]
    runner = Runner(args.api, f"http://{args.monitoring_host}:{server.server_port}", case.alerting, services,
                    args.companies, args.people, args.seconds, args.alert_every, args.think)
    result = asyncio.run(runner.run(args.drain_timeout))
    server.shutdown()
    print(format_report(result))
    if args.json:
        args.json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    ok = result["server_errors"] == 0 and not result["separation_violations"] and \
        result["investigations_unfinished"] == 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
