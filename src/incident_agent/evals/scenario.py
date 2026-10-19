"""Reproducible incident scenarios with known ground truth."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from incident_agent.domain.models import ActionType, Environment, RiskLevel
from incident_agent.verification.evaluator import (
    Observation,
    SuccessCriterion,
    TestResult,
    VerificationStatus,
)

# Fixed reference time so every run is identical.
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Series(_Model):
    """Evenly spaced samples of one signal, starting at the action's completion."""

    signal: str
    interval_seconds: int = Field(gt=0)
    values: tuple[float, ...]
    start_offset_seconds: int = 0

    def observations(self, scenario_id: str, variant_id: str) -> list[Observation]:
        return [
            Observation(
                signal=self.signal,
                at=T0 + timedelta(seconds=self.start_offset_seconds + i * self.interval_seconds),
                value=v,
                evidence_ref=f"{scenario_id}/{variant_id}/{self.signal}#{i}",
            )
            for i, v in enumerate(self.values)
        ]


class ScenarioAction(_Model):
    type: ActionType
    risk: RiskLevel
    reversible: bool
    blast_radius_pct: float
    params: dict[str, Any] = Field(default_factory=dict)


class Variant(_Model):
    id: str
    description: str
    action: ScenarioAction
    # What a naive system sees: the executor reported the action ran fine.
    action_succeeded: bool = True
    # Ground truth: did the service really recover without regressions?
    actually_recovered: bool
    elapsed_seconds: int = Field(ge=0, description="Time since the action completed when verification runs")
    series: tuple[Series, ...] = ()
    tests: tuple[TestResult, ...] = ()
    agent_claim: Optional[str] = None
    expected_policy: Literal["allowed", "blocked"] = "allowed"
    expected_status: Optional[VerificationStatus] = None

    def observations(self, scenario_id: str) -> list[Observation]:
        out: list[Observation] = []
        for s in self.series:
            out.extend(s.observations(scenario_id, self.id))
        return out


class Scenario(_Model):
    id: str
    title: str
    ground_truth_cause: str
    service: str
    environment: Environment
    expected_safe_actions: tuple[ActionType, ...]
    forbidden_actions: tuple[ActionType, ...] = ()
    criteria: tuple[SuccessCriterion, ...]
    variants: tuple[Variant, ...]


def load_scenario(path: Path) -> Scenario:
    with path.open(encoding="utf-8") as f:
        return Scenario.model_validate(yaml.safe_load(f))


def load_scenarios(directory: Path) -> list[Scenario]:
    return [load_scenario(p) for p in sorted(directory.glob("*.yaml"))]
