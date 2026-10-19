"""What an investigation produces: evidence, a timeline, ranked causes, a proposed fix."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from incident_agent.domain.models import ActionSpec, EvidenceKind


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Evidence(_Model):
    """One fact taken from a telemetry system, with a way to reproduce it."""

    id: str  # short and stable inside one investigation, e.g. "E3"
    kind: EvidenceKind
    source: str  # prometheus, loki, jaeger
    service: str
    observed_at: datetime
    locator: str  # the query that produced it
    summary: str  # one human-readable sentence
    facts: dict[str, Any] = Field(default_factory=dict)  # machine-readable details for the reasoning step


class ServiceCall(_Model):
    """One service calling another, as seen in the traces an investigation read."""

    caller: str
    callee: str
    calls: int
    errors: int = 0


class CauseKind(str, Enum):
    BAD_DEPLOY = "bad_deploy"
    DEPENDENCY_FAILURE = "dependency_failure"
    SATURATION = "saturation"


class Hypothesis(_Model):
    cause: CauseKind
    service: str  # where the cause lives (may differ from the alerting service)
    statement: str
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence: tuple[str, ...]
    contradicting_evidence: tuple[str, ...] = ()


class TimelineEvent(_Model):
    at: datetime
    description: str
    evidence_id: str


class Proposal(_Model):
    action: ActionSpec
    rationale: str
    hypothesis_index: int  # which hypothesis it addresses (0 = top)


class AIUsage(_Model):
    """What the optional AI step used and cost, for one investigation."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    priced: bool = True  # False when the model is not in the price table


class Investigation(_Model):
    service: str
    environment: str
    window_start: datetime
    window_end: datetime
    engine: str  # "rules" or "claude:<model>"
    conclusion: Literal["cause_identified", "insufficient_evidence", "no_anomaly"]
    summary: str
    evidence: tuple[Evidence, ...]
    timeline: tuple[TimelineEvent, ...]
    hypotheses: tuple[Hypothesis, ...]
    proposal: Optional[Proposal] = None
    notes: tuple[str, ...] = ()
    duration_seconds: float = 0.0  # wall-clock time of the whole investigation
    ai_usage: Optional[AIUsage] = None  # set when the AI step ran (even if its answer was not used)
    service_calls: tuple[ServiceCall, ...] = ()  # who calls whom, for the company's dependency map

    def evidence_by_id(self, evidence_id: str) -> Optional[Evidence]:
        return next((e for e in self.evidence if e.id == evidence_id), None)
