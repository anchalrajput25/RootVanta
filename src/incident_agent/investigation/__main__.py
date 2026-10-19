"""Investigate a service on the live practice shop.

    python -m incident_agent.investigation checkout
    python -m incident_agent.investigation search --minutes 10 --json report.json
    python -m incident_agent.investigation checkout --ai      # also ask Claude (needs ANTHROPIC_API_KEY)

Reads metrics from Prometheus, logs and changes from Loki, and traces from
Jaeger, then prints what broke, the evidence, and a safe fix. It never
changes anything.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from incident_agent.investigation import llm, report
from incident_agent.investigation.collectors import Collectors
from incident_agent.investigation.graph import investigate
from incident_agent.investigation.rules import Runbook
from incident_agent.telemetry.jaeger import JaegerClient
from incident_agent.telemetry.loki import LokiClient
from incident_agent.telemetry.prometheus import PrometheusClient

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RUNBOOK = PROJECT_ROOT / "demo" / "runbook.yaml"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("service", help="the alerting service, e.g. checkout")
    p.add_argument("--minutes", type=int, default=15, help="how far back to look (default 15)")
    p.add_argument("--environment", default="production")
    p.add_argument("--ai", action="store_true", help="also let Claude rank the causes")
    p.add_argument("--model", default=llm.DEFAULT_MODEL)
    p.add_argument("--json", metavar="FILE", help="also save the full result as JSON")
    p.add_argument("--runbook", type=Path, default=DEFAULT_RUNBOOK)
    p.add_argument("--prometheus", default="http://localhost:9090")
    p.add_argument("--loki", default="http://localhost:3100")
    p.add_argument("--jaeger", default="http://localhost:16686")
    args = p.parse_args(argv)

    collectors = Collectors(PrometheusClient(args.prometheus), LokiClient(args.loki), JaegerClient(args.jaeger))
    inv = investigate(collectors, args.service, minutes=args.minutes, environment=args.environment,
                      runbook=Runbook.load(args.runbook), use_llm=args.ai, model=args.model)
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a narrower code page
    print(report.to_markdown(inv))
    if args.json:
        Path(args.json).write_text(report.dumps(inv), encoding="utf-8")
        print(f"saved {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
