"""Where the workers find monitoring and the systems they may change (environment variables).

For now every organization uses the same connections. Per-organization
connections, with secrets in a vault, come with the integrations work.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from incident_agent.investigation.collectors import Collectors
from incident_agent.investigation.rules import Runbook
from incident_agent.remediation.executor import DemoShopExecutor
from incident_agent.remediation.runner import Timing
from incident_agent.telemetry.jaeger import JaegerClient
from incident_agent.telemetry.loki import LokiClient
from incident_agent.telemetry.prometheus import PrometheusClient

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEMO_SHOP = "checkout=http://localhost:8101,payments=http://localhost:8102,search=http://localhost:8103"


def _endpoints(spec: str) -> dict[str, str]:
    pairs = (item.split("=", 1) for item in spec.split(",") if "=" in item)
    return {name.strip(): url.strip() for name, url in pairs}


@dataclass(frozen=True)
class Connections:
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    jaeger_url: str = "http://localhost:16686"
    shop_endpoints: dict[str, str] = field(default_factory=lambda: _endpoints(DEMO_SHOP))
    shop_token: str = "demo-admin-token"
    runbook_path: Optional[Path] = PROJECT_ROOT / "demo" / "runbook.yaml"
    timing: Timing = field(default_factory=Timing)
    use_llm: bool = False

    @classmethod
    def from_env(cls) -> "Connections":
        env = os.environ.get
        runbook = env("INCIDENT_AGENT_RUNBOOK")
        return cls(
            prometheus_url=env("INCIDENT_AGENT_PROMETHEUS_URL", cls.prometheus_url),
            loki_url=env("INCIDENT_AGENT_LOKI_URL", cls.loki_url),
            jaeger_url=env("INCIDENT_AGENT_JAEGER_URL", cls.jaeger_url),
            shop_endpoints=_endpoints(env("INCIDENT_AGENT_SHOP_URLS", DEMO_SHOP)),
            shop_token=env("INCIDENT_AGENT_SHOP_TOKEN", cls.shop_token),
            runbook_path=Path(runbook) if runbook else cls.runbook_path,
            timing=Timing(
                settle_seconds=int(env("INCIDENT_AGENT_SETTLE_SECONDS", "60")),
                window_seconds=int(env("INCIDENT_AGENT_VERIFY_WINDOW_SECONDS", "600")),
                stage_window_seconds=int(env("INCIDENT_AGENT_STAGE_WINDOW_SECONDS", "120")),
            ),
            use_llm=env("INCIDENT_AGENT_USE_AI", "") == "1",
        )

    def prometheus(self, org_id: str) -> PrometheusClient:
        return PrometheusClient(self.prometheus_url)

    def collectors(self, org_id: str) -> Collectors:
        return Collectors(self.prometheus(org_id), LokiClient(self.loki_url), JaegerClient(self.jaeger_url))

    def executor(self, org_id: str) -> DemoShopExecutor:
        return DemoShopExecutor(self.shop_endpoints, self.shop_token)

    def runbook(self) -> Runbook:
        return Runbook.load(self.runbook_path)
