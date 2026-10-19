"""Check a fix on the live practice shop: did it really work?

    python -m incident_agent.telemetry.check bad_deploy_5xx --window 120 --wait

Takes the success criteria from the scenario file, reads real metrics from
Prometheus after the fix finished (demo/.last_action.json, written by
demo/inject.py, or --since), and runs the same verifier as the scorecard.

Observation starts a short settle period after the fix, because the signals
are 1-minute rates: right after a fix they still include errors from before
it. Error signatures are still compared with the moment of the fix, so an
error that first appears during the settle period counts as new.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from incident_agent.evals.scenario import load_scenario
from incident_agent.telemetry.prometheus import PrometheusClient, PrometheusError, collect_observations
from incident_agent.verification.evaluator import Verdict, VerificationStatus, evaluate_recovery

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCENARIOS = PROJECT_ROOT / "evals" / "scenarios"
LAST_ACTION = PROJECT_ROOT / "demo" / ".last_action.json"
DEFAULT_SETTLE_SECONDS = 60  # matches the [1m] rate windows in the recording rules


def check(
    prom: PrometheusClient,
    scenario_id: str,
    action_completed_at: datetime,
    now: datetime,
    window_seconds: Optional[int] = None,
    step_seconds: int = 15,
    settle_seconds: int = DEFAULT_SETTLE_SECONDS,
) -> Verdict:
    scenario = load_scenario(SCENARIOS / f"{scenario_id}.yaml")
    criteria = [
        c.model_copy(update={"window_seconds": window_seconds}) if window_seconds else c for c in scenario.criteria
    ]
    observe_from = action_completed_at + timedelta(seconds=settle_seconds)
    window_end = observe_from + timedelta(seconds=max(c.window_seconds for c in criteria))
    observations = collect_observations(
        prom, scenario.service, sorted({c.signal for c in criteria}),
        start=observe_from, end=min(now, window_end), step_seconds=step_seconds, baseline_at=action_completed_at,
    ) if now > observe_from else []
    return evaluate_recovery(criteria, observations, [], action_completed_at=observe_from, evaluated_at=now)


def format_verdict(v: Verdict) -> str:
    lines = [f"status: {v.status.value.upper()}", v.summary, f"observed from {v.action_completed_at.isoformat()}", ""]
    for c in v.checks:
        flag = "" if c.required else " (optional)"
        lines.append(f"  [{c.outcome.value:<17}] {c.check_id}{flag}: {c.reason}  ({c.samples} samples)")
    if v.should_roll_back:
        lines += ["", "recommendation: roll back this fix"]
    return "\n".join(lines)


def _last_action(scenario_id: str) -> datetime:
    if not LAST_ACTION.exists():
        sys.exit("no recorded fix found; run a fix with demo/inject.py first, or pass --since")
    data = json.loads(LAST_ACTION.read_text())
    if data["scenario"] != scenario_id:
        sys.exit(f"the last fix was for {data['scenario']}, not {scenario_id}; pass --since to override")
    return datetime.fromisoformat(data["completed_at"])


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("scenario", help="scenario id, e.g. bad_deploy_5xx")
    p.add_argument("--prometheus", default="http://localhost:9090")
    p.add_argument("--since", type=datetime.fromisoformat, help="when the fix finished (ISO time, UTC)")
    p.add_argument("--window", type=int, help="shorten every observation window to this many seconds (demos)")
    p.add_argument("--settle", type=int, default=DEFAULT_SETTLE_SECONDS,
                   help="seconds after the fix before observation starts (default %(default)s)")
    p.add_argument("--wait", action="store_true", help="wait until the observation window is over")
    a = p.parse_args(argv)

    since = a.since or _last_action(a.scenario)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    prom = PrometheusClient(a.prometheus)
    if a.wait:
        window = a.window or max(c.window_seconds for c in load_scenario(SCENARIOS / f"{a.scenario}.yaml").criteria)
        # A little extra so the last samples are scraped and recorded.
        remaining = (since + timedelta(seconds=a.settle + window + 20) - datetime.now(timezone.utc)).total_seconds()
        if remaining > 0:
            print(f"waiting {remaining:.0f}s for the observation window to finish...", flush=True)
            time.sleep(remaining)
    try:
        verdict = check(prom, a.scenario, since, datetime.now(timezone.utc), a.window, settle_seconds=a.settle)
    except PrometheusError as e:
        sys.exit(f"{e}\nIs the demo running? Start it from the demo folder with: docker compose up -d")
    print(format_verdict(verdict))
    return 0 if verdict.status == VerificationStatus.VERIFIED else 1


if __name__ == "__main__":
    sys.exit(main())
