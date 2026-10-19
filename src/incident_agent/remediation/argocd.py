"""Argo CD: roll a service back to an earlier synced revision, and read deploy history.

Each service maps to one Argo CD application (by default an application with
the same name). A rollback asks Argo CD to sync the application to an entry
of its own history, waits for the sync to finish, and reads the result back.
The undo is a rollback to the revision that was running before.

Argo CD refuses a rollback while auto-sync is on, because it would put the
latest revision straight back. The agent does not turn auto-sync off on its
own; it says so in the practice run, and a person decides.

The same history is evidence: a sync shortly before an incident is a deploy,
even when the service writes no log line about it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import httpx

from incident_agent.domain.models import ActionSpec, ActionType
from incident_agent.remediation.executor import ExecutorError, Preview, _undo

SYNC_TIMEOUT_SECONDS = 180
POLL_SECONDS = 3
# Argo CD application names are Kubernetes names. Checked so a name can never change the API path.
APP_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")


def check_app_name(name: str) -> str:
    if not APP_NAME.match(name):
        raise ExecutorError(f"{name!r} is not a valid Argo CD application name")
    return name


def short(revision: str) -> str:
    """Git revisions are shown like git does; other versions (Helm chart versions) as they are."""
    return revision[:7] if len(revision) == 40 and all(c in "0123456789abcdef" for c in revision) else revision


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class HistoryEntry:
    id: int
    revision: str
    deployed_at: Optional[datetime]


@dataclass
class ArgoCDClient:
    url: str
    http: httpx.Client

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict[str, Any]:
        try:
            r = self.http.request(method, f"{self.url.rstrip('/')}/api/v1{path}", json=body)
        except httpx.HTTPError as e:
            raise ExecutorError(f"Argo CD could not be reached: {e}") from e
        if r.status_code in (401, 403):
            raise ExecutorError(f"Argo CD refused the token ({r.status_code}). It needs get and sync rights "
                                "on the application.")
        if r.status_code == 404:
            raise ExecutorError(f"Argo CD has no application at {path.split('/')[2]}")
        if r.status_code >= 400:
            try:
                message = r.json().get("message") or r.text
            except ValueError:
                message = r.text
            raise ExecutorError(f"Argo CD said no ({r.status_code}): {str(message)[:300]}")
        try:
            return r.json()
        except ValueError as e:
            raise ExecutorError("Argo CD answered with something that is not JSON") from e

    def application(self, name: str) -> dict[str, Any]:
        return self._request("GET", f"/applications/{name}")

    def applications(self) -> list[str]:
        items = self._request("GET", "/applications?fields=items.metadata.name").get("items") or []
        return [i["metadata"]["name"] for i in items if (i.get("metadata") or {}).get("name")]

    def rollback(self, name: str, history_id: int) -> dict[str, Any]:
        return self._request("POST", f"/applications/{name}/rollback",
                             {"name": name, "id": history_id, "dryRun": False, "prune": False})


def history(app: dict[str, Any]) -> list[HistoryEntry]:
    out = []
    for h in (app.get("status") or {}).get("history") or []:
        revision = h.get("revision") or next(iter(h.get("revisions") or []), "")
        if isinstance(h.get("id"), int) and revision:
            out.append(HistoryEntry(h["id"], revision, _parse_time(h.get("deployedAt"))))
    return out


def current_revision(app: dict[str, Any]) -> str:
    status = app.get("status") or {}
    revision = (status.get("sync") or {}).get("revision")
    if revision:
        return revision
    entries = history(app)
    if entries:
        return entries[-1].revision
    raise ExecutorError("Argo CD reports no synced revision for this application yet")


def auto_sync_on(app: dict[str, Any]) -> bool:
    return bool(((app.get("spec") or {}).get("syncPolicy") or {}).get("automated"))


def find_entry(entries: list[HistoryEntry], target: str) -> Optional[HistoryEntry]:
    """The newest history entry for ``target``: a full revision, a short one (7+ characters), or a chart version."""
    for e in reversed(entries):
        if e.revision == target or (len(target) >= 7 and e.revision.startswith(target)):
            return e
    return None


@dataclass
class ArgoCDExecutor:
    client: ArgoCDClient
    apps: dict[str, str] = field(default_factory=dict)  # service -> Argo CD application; default: same name
    timeout_seconds: float = SYNC_TIMEOUT_SECONDS
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def app_name(self, service: str) -> str:
        return check_app_name(self.apps.get(service) or service)

    def preview(self, action: ActionSpec) -> Preview:
        if action.type != ActionType.ROLLBACK_DEPLOYMENT:
            raise ExecutorError(f"Argo CD only handles rollbacks, not {action.type.value}")
        name = self.app_name(action.service)
        app = self.client.application(name)
        now, target = current_revision(app), str(action.params["target_version"])
        entry = find_entry(history(app), target)
        if entry is None:
            known = ", ".join(short(e.revision) for e in history(app)[-5:]) or "none"
            raise ExecutorError(f"{target} is not in the Argo CD history of {name} (recent revisions: {known})")
        if auto_sync_on(app):
            raise ExecutorError(f"Auto-sync is on for the Argo CD application {name}, so Argo CD would undo a "
                                "rollback straight away. Turn auto-sync off for it, or revert the change in git.")
        before, after = {"version": now}, {"version": entry.revision}
        no_op = now == entry.revision
        change = f"{action.service}: Argo CD application {name} {short(now)} -> {short(entry.revision)}"
        return Preview(action=action, before=before, after=after,
                       changes=[change + (" (already in place)" if no_op else "")],
                       undo=_undo(action, {"target_version": now}), no_op=no_op)

    def apply(self, action: ActionSpec) -> dict[str, Any]:
        if action.type != ActionType.ROLLBACK_DEPLOYMENT:
            raise ExecutorError(f"Argo CD only handles rollbacks, not {action.type.value}")
        name = self.app_name(action.service)
        app = self.client.application(name)
        entry = find_entry(history(app), str(action.params["target_version"]))
        if entry is None:
            raise ExecutorError(f"{action.params['target_version']} is not in the Argo CD history of {name}")
        if current_revision(app) == entry.revision:
            return {"version": entry.revision}
        if auto_sync_on(app):
            raise ExecutorError(f"Auto-sync is on for the Argo CD application {name}; rollback refused")
        self.client.rollback(name, entry.id)
        return self._wait(name, entry.revision)

    def _wait(self, name: str, revision: str) -> dict[str, Any]:
        """Wait until Argo CD has finished syncing to ``revision``."""
        deadline = self.clock() + self.timeout_seconds
        while True:
            app = self.client.application(name)
            op = (app.get("status") or {}).get("operationState") or {}
            phase = op.get("phase")
            if phase in ("Failed", "Error"):
                raise ExecutorError(f"Argo CD sync of {name} {phase.lower()}: {str(op.get('message', ''))[:300]}")
            if phase == "Succeeded" and current_revision(app) == revision:
                health = ((app.get("status") or {}).get("health") or {}).get("status")
                return {"version": revision, "health": health}
            if self.clock() >= deadline:
                raise ExecutorError(f"Argo CD did not finish syncing {name} to {short(revision)} within "
                                    f"{self.timeout_seconds:g} s")
            self.sleep(POLL_SECONDS)


@dataclass(frozen=True)
class Deploy:
    service: str
    at: datetime
    from_version: str
    to_version: str


@dataclass
class ArgoCDDeploys:
    """Reads deploys from Argo CD history, as evidence for investigations."""

    client: ArgoCDClient
    apps: dict[str, str]  # service -> application; when empty, every application, named like its service

    def deploys(self, start: datetime, end: datetime) -> list[Deploy]:
        apps = self.apps or {name: name for name in self.client.applications()}
        out = []
        for service, name in sorted(apps.items()):
            entries = history(self.client.application(check_app_name(name or service)))
            for prev, cur in zip(entries, entries[1:]):
                if cur.deployed_at and start <= cur.deployed_at <= end and prev.revision != cur.revision:
                    out.append(Deploy(service, cur.deployed_at, prev.revision, cur.revision))
        return out
