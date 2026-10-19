"""Kubernetes: change how many copies of a service run.

Each service maps to one Deployment, written ``namespace/name``. The agent
uses the Deployment's ``scale`` subresource, so its service account only
needs ``get`` and ``patch`` on ``deployments/scale``, nothing else.

When the agent itself runs in the cluster (the Helm chart), the address
``https://kubernetes.default.svc`` with no token uses the pod's own service
account and the cluster's certificate.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from incident_agent.domain.models import ActionSpec, ActionType
from incident_agent.remediation.executor import ExecutorError, Preview, _undo

IN_CLUSTER_URL = "https://kubernetes.default.svc"
SERVICE_ACCOUNT_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
TIMEOUT = 10.0
# Kubernetes object names (RFC 1123). Checked so a name can never change the API path.
NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")


def in_cluster_http(token: Optional[str], sa_dir: str = SERVICE_ACCOUNT_DIR) -> httpx.Client:
    """An HTTP client for the cluster the agent runs in, signed in as its service account."""
    if not token:
        try:
            with open(os.path.join(sa_dir, "token")) as f:
                token = f.read().strip()
        except OSError as e:
            raise ExecutorError("No token given and this is not running inside Kubernetes "
                                "(no service account token found)") from e
    ca = os.path.join(sa_dir, "ca.crt")
    return httpx.Client(timeout=TIMEOUT, headers={"Authorization": f"Bearer {token}"},
                        verify=ca if os.path.exists(ca) else True)


def split_target(target: str) -> tuple[str, str]:
    namespace, slash, name = target.strip().partition("/")
    if not slash:
        namespace, name = "default", namespace
    if not NAME.match(namespace) or not NAME.match(name):
        raise ExecutorError(f"{target!r} is not a Deployment written as namespace/name")
    return namespace, name


@dataclass
class KubernetesExecutor:
    url: str
    http: httpx.Client
    deployments: dict[str, str]  # service -> "namespace/name"

    def _path(self, service: str) -> str:
        target = self.deployments.get(service)
        if not target:
            raise ExecutorError(f"no Kubernetes Deployment is set for {service}")
        namespace, name = split_target(target)
        return f"{self.url.rstrip('/')}/apis/apps/v1/namespaces/{namespace}/deployments/{name}/scale"

    def _call(self, method: str, service: str, body: Optional[dict] = None) -> dict[str, Any]:
        path = self._path(service)
        headers = {"Content-Type": "application/merge-patch+json"} if body is not None else None
        try:
            r = self.http.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise ExecutorError(f"Kubernetes could not be reached: {e}") from e
        if r.status_code in (401, 403):
            raise ExecutorError(f"Kubernetes refused the request ({r.status_code}). The service account needs "
                                "get and patch on deployments/scale.")
        if r.status_code == 404:
            raise ExecutorError(f"Kubernetes has no Deployment {self.deployments[service]}")
        if r.status_code >= 400:
            raise ExecutorError(f"Kubernetes said no ({r.status_code}): {r.text[:300]}")
        try:
            return r.json()
        except ValueError as e:
            raise ExecutorError("Kubernetes answered with something that is not JSON") from e

    def replicas(self, service: str) -> int:
        return int((self._call("GET", service).get("spec") or {}).get("replicas", 0))

    def preview(self, action: ActionSpec) -> Preview:
        if action.type != ActionType.SCALE_SERVICE:
            raise ExecutorError(f"Kubernetes here only changes replica counts, not {action.type.value}")
        now, want = self.replicas(action.service), int(action.params["replicas"])
        no_op = now == want
        change = f"{action.service}: replicas {now} -> {want} ({self.deployments[action.service]})"
        return Preview(action=action, before={"replicas": now}, after={"replicas": want},
                       changes=[change + (" (already in place)" if no_op else "")],
                       undo=_undo(action, {"replicas": now}), no_op=no_op)

    def apply(self, action: ActionSpec) -> dict[str, Any]:
        if action.type != ActionType.SCALE_SERVICE:
            raise ExecutorError(f"Kubernetes here only changes replica counts, not {action.type.value}")
        want = int(action.params["replicas"])
        s = self._call("PATCH", action.service, {"spec": {"replicas": want}})
        got = (s.get("spec") or {}).get("replicas")
        if got != want:
            raise ExecutorError(f"Kubernetes reports {got} replicas for {action.service}, not {want}")
        return {"replicas": got}
