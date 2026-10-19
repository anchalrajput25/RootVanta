"""Apply recovery actions to a real system, with a practice run and an undo for each.

An executor only knows how to change things. Whether a change may happen at
all is decided earlier by the safety policy and the approvals; whether it
worked is decided later by the verifier.

``DemoShopExecutor`` drives the practice shop's admin API. Other targets
(Kubernetes, Argo CD, LaunchDarkly…) implement the same two methods.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

import httpx

from incident_agent.domain.models import ActionSpec, ActionType


class ExecutorError(RuntimeError):
    pass


@dataclass(frozen=True)
class Preview:
    """What an action would change, worked out from the system's current state."""

    action: ActionSpec
    before: dict[str, Any]
    after: dict[str, Any]
    changes: list[str]
    undo: ActionSpec  # puts ``before`` back
    no_op: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action.model_dump(mode="json"), "before": self.before, "after": self.after,
                "changes": self.changes, "undo": self.undo.model_dump(mode="json"), "no_op": self.no_op}


class Executor(Protocol):
    def preview(self, action: ActionSpec) -> Preview: ...

    def apply(self, action: ActionSpec) -> dict[str, Any]:
        """Make the change and return the state read back afterwards. Raises ExecutorError."""
        ...


def _undo(action: ActionSpec, params: dict[str, Any]) -> ActionSpec:
    return action.model_copy(update={"params": params})


@dataclass
class DemoShopExecutor:
    endpoints: dict[str, str]  # service name -> base URL of its admin API
    token: str
    http: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=10.0))

    def _call(self, service: str, path: str, body: Optional[dict] = None) -> dict[str, Any]:
        base = self.endpoints.get(service)
        if base is None:
            raise ExecutorError(f"no admin endpoint configured for {service}")
        try:
            r = self.http.request("POST" if body is not None else "GET", f"{base.rstrip('/')}{path}",
                                  json=body, headers={"X-Admin-Token": self.token})
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ExecutorError(f"{service} admin API failed on {path}: {e}") from e

    def state(self, service: str) -> dict[str, Any]:
        return self._call(service, "/admin/state")

    def preview(self, action: ActionSpec) -> Preview:
        s = self.state(action.service)
        p = action.params
        if action.type == ActionType.ROLLBACK_DEPLOYMENT:
            before, after = {"version": s["version"]}, {"version": p["target_version"]}
            change = f"{action.service}: version {before['version']} -> {after['version']}"
            undo = {"target_version": before["version"]}
        elif action.type == ActionType.TOGGLE_FEATURE_FLAG:
            name = p["flag"]
            value = bool(s.get("flags", {}).get(name, False))
            pct = float(s.get("flag_rollout", {}).get(name, 100.0)) if value else 0.0
            before = {"flag": name, "value": value, "rollout_pct": pct}
            after = {"flag": name, "value": bool(p["value"]), "rollout_pct": float(p.get("rollout_pct", 100))}
            change = (f"{action.service}: flag {name} {value} ({pct:g}% of requests) -> "
                      f"{after['value']} ({after['rollout_pct']:g}% of requests)")
            undo = {"flag": name, "value": value, "rollout_pct": pct if value else 100.0}
        elif action.type == ActionType.SCALE_SERVICE:
            before, after = {"replicas": s["replicas"]}, {"replicas": p["replicas"]}
            change = f"{action.service}: replicas {before['replicas']} -> {after['replicas']}"
            undo = {"replicas": before["replicas"]}
        else:
            raise ExecutorError(f"{action.type.value} is not supported on the practice shop")
        no_op = before == after
        return Preview(action=action, before=before, after=after,
                       changes=[change + (" (already in place)" if no_op else "")],
                       undo=_undo(action, undo), no_op=no_op)

    def apply(self, action: ActionSpec) -> dict[str, Any]:
        p = action.params
        if action.type == ActionType.ROLLBACK_DEPLOYMENT:
            s = self._call(action.service, "/admin/deploy", {"version": p["target_version"]})
            ok = s.get("version") == p["target_version"]
        elif action.type == ActionType.TOGGLE_FEATURE_FLAG:
            body = {"flag": p["flag"], "value": bool(p["value"]), "rollout_pct": float(p.get("rollout_pct", 100))}
            s = self._call(action.service, "/admin/flags", body)
            ok = s.get("flags", {}).get(p["flag"]) == body["value"]
        elif action.type == ActionType.SCALE_SERVICE:
            s = self._call(action.service, "/admin/scale", {"replicas": p["replicas"]})
            ok = s.get("replicas") == p["replicas"]
        else:
            raise ExecutorError(f"{action.type.value} is not supported on the practice shop")
        if not ok:
            raise ExecutorError(f"{action.service} did not report the new state after {action.type.value}")
        return s
