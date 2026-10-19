"""Start a background worker.

    python -m incident_agent.workflow            # production timing: 10-minute checks
    python -m incident_agent.workflow --demo     # practice shop: 2-minute checks

Settings come from environment variables (see .env.example). Start several
workers to handle several incidents at once.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import signal
import socket
import time

from incident_agent.db.session import init_db, make_engine, make_session_factory
from incident_agent.integrations.crypto import SecretBox
from incident_agent.integrations.store import OrgConnections
from incident_agent.remediation.runner import Timing
from incident_agent.workflow.config import Connections
from incident_agent.workflow.worker import WorkerDeps, run_once

IDLE_SECONDS = 2.0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--demo", action="store_true", help="short checks for the practice shop (2 min, 1 min per stage)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per HTTP call is too noisy
    engine = make_engine(os.environ.get("INCIDENT_AGENT_DATABASE_URL", "sqlite:///./incident_agent.db"))
    init_db(engine)
    conn = Connections.from_env()
    if args.demo:
        conn = dataclasses.replace(conn, timing=Timing(settle_seconds=60, window_seconds=120, stage_window_seconds=60))
    session_factory = make_session_factory(engine)
    # Each company's own monitoring, saved in Settings. A single-company install may share
    # the environment's connections instead (INCIDENT_AGENT_SHARED_CONNECTIONS=1).
    shared = conn if os.environ.get("INCIDENT_AGENT_SHARED_CONNECTIONS") == "1" else None
    per_org = OrgConnections(session_factory, SecretBox.from_env(), fallback=shared)
    deps = WorkerDeps(session_factory=session_factory, collectors=per_org.collectors,
                      prometheus=per_org.prometheus, executor=per_org.executor, runbook=conn.runbook(),
                      timing=conn.timing, use_llm=conn.use_llm)
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        logging.info("stopping after the current job")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    t = conn.timing
    logging.info("worker %s ready (checks: settle %ss, window %ss, stage %ss)", worker_id,
                 t.settle_seconds, t.window_seconds, t.stage_window_seconds)
    while not stopping:
        if not run_once(deps, worker_id):
            time.sleep(IDLE_SECONDS)


if __name__ == "__main__":
    main()
