"""Core domain models.

Every record that belongs to a customer carries ``org_id`` (and ``team_id``
where it applies) so the system can serve many companies at once and enforce
tenant isolation in code and, later, in the database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------
# Tenancy and identity
# --------------------------------------------------------------------------


class Role(str, Enum):
    ADMIN = "admin"
    APPROVER = "approver"
    RESPONDER = "responder"
    VIEWER = "viewer"


class Organization(_Model):
    id: str = Field(default_factory=lambda: new_id("org"))
    name: str


class Team(_Model):
    id: str = Field(default_factory=lambda: new_id("team"))
    org_id: str
    name: str


class User(_Model):
    id: str = Field(default_factory=lambda: new_id("usr"))
    org_id: str
    team_ids: frozenset[str] = frozenset()
    email: str
    roles: frozenset[Role] = frozenset({Role.VIEWER})

    def has_role(self, *roles: Role) -> bool:
        return Role.ADMIN in self.roles or any(r in self.roles for r in roles)


# --------------------------------------------------------------------------
# Incident and evidence
# --------------------------------------------------------------------------


class Environment(str, Enum):
    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"


class Severity(str, Enum):
    SEV1 = "sev1"
    SEV2 = "sev2"
    SEV3 = "sev3"
    SEV4 = "sev4"


class IncidentStatus(str, Enum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    MITIGATING = "mitigating"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    CLOSED = "closed"


class Incident(_Model):
    id: str = Field(default_factory=lambda: new_id("inc"))
    org_id: str
    team_id: str
    title: str
    service: str
    environment: Environment
    severity: Severity
    started_at: datetime
    status: IncidentStatus = IncidentStatus.OPEN


class EvidenceKind(str, Enum):
    METRIC = "metric"
    LOG = "log"
    TRACE = "trace"
    DEPLOYMENT = "deployment"
    INFRASTRUCTURE = "infrastructure"
    TEST = "test"


class EvidenceItem(_Model):
    id: str = Field(default_factory=lambda: new_id("ev"))
    org_id: str
    incident_id: str
    kind: EvidenceKind
    source: str  # integration that produced it, e.g. "prometheus"
    observed_at: datetime
    locator: str  # query or link that reproduces the evidence
    summary: str  # sanitized, human-readable


class Hypothesis(_Model):
    id: str = Field(default_factory=lambda: new_id("hyp"))
    org_id: str
    incident_id: str
    statement: str
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# Recovery actions
# --------------------------------------------------------------------------


class ActionType(str, Enum):
    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    RESTART_SERVICE = "restart_service"
    SCALE_SERVICE = "scale_service"
    TOGGLE_FEATURE_FLAG = "toggle_feature_flag"
    DELETE_RESOURCE = "delete_resource"
    RUN_SHELL = "run_shell"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2}[self.value]


class ActionSpec(_Model):
    """A concrete, typed action. Never free-form text from a model."""

    type: ActionType
    service: str
    environment: Environment
    params: dict[str, Any] = Field(default_factory=dict)
    risk: RiskLevel
    reversible: bool
    blast_radius_pct: float = Field(ge=0.0, le=100.0, description="Share of the service's capacity affected")
    rollback: Optional["ActionSpec"] = None


class RecoveryPlan(_Model):
    id: str = Field(default_factory=lambda: new_id("plan"))
    org_id: str
    team_id: str
    incident_id: str
    hypothesis_id: Optional[str] = None
    action: ActionSpec
    expected_effect: str
    prerequisites: tuple[str, ...] = ()
    requested_by: str  # user id


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class Approval(_Model):
    id: str = Field(default_factory=lambda: new_id("apr"))
    org_id: str
    plan_id: str
    approver_id: str
    decision: ApprovalDecision
    decided_at: datetime = Field(default_factory=utcnow)


class AuditEvent(_Model):
    id: str = Field(default_factory=lambda: new_id("aud"))
    org_id: str
    actor: str
    event: str
    object_id: str
    at: datetime = Field(default_factory=utcnow)
    details: dict[str, Any] = Field(default_factory=dict)
