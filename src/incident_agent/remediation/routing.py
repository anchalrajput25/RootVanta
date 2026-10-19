"""Send each kind of change to the system that can make it.

A company may roll back through Argo CD, scale through Kubernetes and flip
flags through its own admin API. Each action goes to the first connected
system that handles its type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from incident_agent.domain.models import ActionSpec, ActionType
from incident_agent.remediation.executor import Executor, ExecutorError, Preview


@dataclass
class RoutingExecutor:
    routes: dict[ActionType, Executor]

    def _for(self, action: ActionSpec) -> Executor:
        executor = self.routes.get(action.type)
        if executor is None:
            raise ExecutorError(f"No connected system can do {action.type.value.replace('_', ' ')}. "
                                "An admin can connect one in Settings > Monitoring.")
        return executor

    def preview(self, action: ActionSpec) -> Preview:
        return self._for(action).preview(action)

    def apply(self, action: ActionSpec) -> dict[str, Any]:
        return self._for(action).apply(action)
