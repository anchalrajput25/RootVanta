"""Builds the monitoring clients and the change system for one company from what it saved.

Workers and the API ask this for an organization's connections. A company that
has not connected Prometheus yet gets a clear error instead of someone else's data.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from incident_agent.db.models import IntegrationRow
from incident_agent.integrations.crypto import SecretBox
from incident_agent.investigation.collectors import Collectors
from incident_agent.domain.models import ActionType
from incident_agent.remediation.argocd import ArgoCDClient, ArgoCDDeploys, ArgoCDExecutor
from incident_agent.remediation.executor import DemoShopExecutor, Executor, ExecutorError
from incident_agent.remediation.kubernetes import IN_CLUSTER_URL, KubernetesExecutor, in_cluster_http
from incident_agent.remediation.routing import RoutingExecutor
from incident_agent.telemetry.jaeger import JaegerClient
from incident_agent.telemetry.loki import LokiClient
from incident_agent.telemetry.prometheus import PrometheusClient

KINDS = ("prometheus", "loki", "jaeger", "service_admin", "argocd", "kubernetes")
TIMEOUT = 10.0


class NotConnected(RuntimeError):
    """The company has not connected a system the work needs."""


@dataclass(frozen=True)
class Integration:
    kind: str
    url: str = ""
    endpoints: dict[str, str] = field(default_factory=dict)
    username: Optional[str] = None
    token: Optional[str] = None

    def http(self) -> httpx.Client:
        """An HTTP client that signs in the way this connection needs."""
        if self.kind == "kubernetes" and self.url.rstrip("/") == IN_CLUSTER_URL:
            try:
                return in_cluster_http(self.token)
            except ExecutorError:
                return httpx.Client(timeout=TIMEOUT)  # not in a cluster: requests are refused with a clear 401
        if self.kind == "service_admin":
            headers = {"X-Admin-Token": self.token} if self.token else {}
            return httpx.Client(timeout=TIMEOUT, headers=headers)
        if self.username and self.token:
            return httpx.Client(timeout=TIMEOUT, auth=(self.username, self.token))
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return httpx.Client(timeout=TIMEOUT, headers=headers)


def check_url(url: str) -> None:
    """Refuse addresses no company should point the agent at.

    Cloud metadata addresses (169.254.x.x) are always refused. A hosted service
    shared by many companies should also set INCIDENT_AGENT_BLOCK_PRIVATE_URLS=1,
    so nobody can use the agent to reach its own internal network."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("the address must start with http:// or https://")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return  # unknown name: the connection test will say it cannot be reached
    block_private = os.environ.get("INCIDENT_AGENT_BLOCK_PRIVATE_URLS") == "1"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise ValueError(f"{parsed.hostname} points to an address that is not allowed")
        if block_private and (ip.is_private or ip.is_loopback):
            raise ValueError(f"{parsed.hostname} points to a private address, which this server does not allow")


class OrgConnections:
    """Per-company connections, with an optional shared fallback for a single-company install."""

    def __init__(self, session_factory: sessionmaker[Session], box: SecretBox, fallback: Any = None):
        self.session_factory = session_factory
        self.box = box
        self.fallback = fallback

    def load(self, org_id: str) -> dict[str, Integration]:
        with self.session_factory() as db:
            db.info["org_id"] = org_id
            rows = db.scalars(select(IntegrationRow).where(IntegrationRow.org_id == org_id)).all()
            return {r.kind: self.to_integration(r) for r in rows}

    def to_integration(self, row: IntegrationRow) -> Integration:
        return Integration(kind=row.kind, url=row.url, endpoints=dict(row.endpoints or {}), username=row.username,
                           token=self.box.open(row.secret) if row.secret else None)

    def prometheus(self, org_id: str) -> PrometheusClient:
        i = self.load(org_id).get("prometheus")
        if i is not None:
            return PrometheusClient(i.url, http=i.http())
        if self.fallback is not None:
            return self.fallback.prometheus(org_id)
        raise NotConnected("Prometheus is not connected. An admin can connect it in Settings > Monitoring.")

    def collectors(self, org_id: str) -> Collectors:
        saved = self.load(org_id)
        if "prometheus" not in saved and self.fallback is not None:
            return self.fallback.collectors(org_id)
        loki, jaeger, argocd = saved.get("loki"), saved.get("jaeger"), saved.get("argocd")
        return Collectors(
            prometheus=self.prometheus(org_id),
            loki=LokiClient(loki.url, http=loki.http()) if loki else None,
            jaeger=JaegerClient(jaeger.url, http=jaeger.http()) if jaeger else None,
            deploys=ArgoCDDeploys(ArgoCDClient(argocd.url, argocd.http()), argocd.endpoints) if argocd else None,
        )

    def executor(self, org_id: str) -> Executor:
        """Each kind of change goes to the system that makes it: rollbacks to Argo CD and
        scaling to Kubernetes when connected, everything else to the service admin API."""
        saved = self.load(org_id)
        routes: dict[ActionType, Executor] = {}
        admin = saved.get("service_admin")
        if admin is not None:
            shop = DemoShopExecutor(admin.endpoints, admin.token or "", http=admin.http())
            routes.update({t: shop for t in (ActionType.ROLLBACK_DEPLOYMENT, ActionType.TOGGLE_FEATURE_FLAG,
                                             ActionType.SCALE_SERVICE)})
        argocd = saved.get("argocd")
        if argocd is not None:
            routes[ActionType.ROLLBACK_DEPLOYMENT] = ArgoCDExecutor(ArgoCDClient(argocd.url, argocd.http()),
                                                                    argocd.endpoints)
        k8s = saved.get("kubernetes")
        if k8s is not None:
            routes[ActionType.SCALE_SERVICE] = KubernetesExecutor(k8s.url, k8s.http(), k8s.endpoints)
        if routes:
            return RoutingExecutor(routes)
        if self.fallback is not None:
            return self.fallback.executor(org_id)
        raise NotConnected("No change system is connected, so fixes cannot be previewed or applied. "
                           "An admin can connect one in Settings > Monitoring.")
