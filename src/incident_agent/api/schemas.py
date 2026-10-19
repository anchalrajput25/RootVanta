"""Request and response bodies."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from incident_agent.domain.models import (
    ActionType,
    ApprovalDecision,
    Environment,
    IncidentStatus,
    RiskLevel,
    Role,
    Severity,
)
from incident_agent.verification.evaluator import Observation, SuccessCriterion, TestResult


Email = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth -------------------------------------------------------------------


class SignupIn(_In):
    organization_name: str = Field(min_length=1, max_length=200)
    email: Email
    password: str = Field(min_length=10, max_length=200)


class LoginIn(_In):
    email: Email
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    org_id: str


class MeOut(BaseModel):
    id: str
    org_id: str
    email: str
    roles: list[Role]
    team_ids: list[str]


# --- organization -----------------------------------------------------------


class TeamIn(_In):
    name: str = Field(min_length=1, max_length=200)


class TeamOut(_Out):
    id: str
    name: str


class UserIn(_In):
    email: Email
    password: str = Field(min_length=10, max_length=200)
    roles: list[Role] = Field(min_length=1)
    team_ids: list[str] = []


class UserOut(BaseModel):
    id: str
    email: str
    roles: list[Role]
    team_ids: list[str]
    is_active: bool


# --- incidents and plans ------------------------------------------------------


class IncidentIn(_In):
    team_id: str
    title: str = Field(min_length=1, max_length=300)
    service: str = Field(min_length=1, max_length=200)
    environment: Environment
    severity: Severity
    started_at: datetime


class IncidentOut(_Out):
    id: str
    team_id: str
    title: str
    service: str
    environment: Environment
    severity: Severity
    status: IncidentStatus
    started_at: datetime
    created_by: str


class ActionIn(_In):
    type: ActionType
    risk: RiskLevel
    reversible: bool
    blast_radius_pct: float = Field(ge=0, le=100)
    params: dict[str, Any] = {}


class PlanIn(_In):
    action: ActionIn
    expected_effect: str = Field(min_length=1)


class PlanOut(_Out):
    id: str
    incident_id: str
    team_id: str
    action: dict[str, Any]
    expected_effect: str
    requested_by: str
    status: str
    policy_decision: dict[str, Any]


class ApprovalIn(_In):
    decision: ApprovalDecision
    comment: Optional[str] = Field(default=None, max_length=2000)


class ApprovalOut(_Out):
    id: str
    plan_id: str
    approver_id: str
    decision: ApprovalDecision
    comment: Optional[str]
    decided_at: datetime


class VerificationIn(_In):
    criteria: list[SuccessCriterion] = Field(min_length=1)
    observations: list[Observation] = []
    test_results: list[TestResult] = []
    action_completed_at: datetime
    evaluated_at: Optional[datetime] = None
    agent_claim: Optional[str] = None


class VerificationOut(_Out):
    id: str
    plan_id: str
    status: str
    verdict: dict[str, Any]
    created_at: datetime


class AuditOut(_Out):
    id: str
    actor_id: Optional[str]
    event: str
    object_type: str
    object_id: str
    details: dict[str, Any]
    at: datetime


# --- step 5: investigations, execution, alerts, notifications ------------------------


class InvestigationIn(_In):
    minutes: int = Field(15, ge=1, le=180)


class InvestigationOut(_Out):
    id: str
    incident_id: str
    job_id: str
    status: str
    conclusion: Optional[str]
    summary: Optional[str]
    plan_id: Optional[str]
    report_markdown: Optional[str]
    report: Optional[dict[str, Any]]
    created_at: datetime


class ExecutionOut(_Out):
    id: str
    plan_id: str
    job_id: str
    status: str
    steps: list[dict[str, Any]]
    preview: Optional[dict[str, Any]]
    requested_by: str
    created_at: datetime
    finished_at: Optional[datetime]


class JobOut(_Out):
    id: str
    kind: str
    status: str
    attempts: int
    error: Optional[str]
    created_at: datetime
    finished_at: Optional[datetime]


class AlertSourceIn(_In):
    name: str = Field(min_length=1, max_length=200)
    team_id: str
    environment: Environment = Environment.PRODUCTION


class AlertSourceOut(_Out):
    id: str
    name: str
    team_id: str
    environment: Environment
    created_at: datetime


class AlertSourceCreated(AlertSourceOut):
    token: str  # shown once; only its hash is stored


class NotificationTargetIn(_In):
    url: str = Field(pattern=r"^https?://", max_length=2000)


class NotificationTargetOut(BaseModel):
    id: str
    url_hint: str  # the full URL is a secret, so only its start and end are shown
    created_at: datetime


class AlertsReceived(BaseModel):
    incidents_opened: list[str]
    grouped_into_existing: list[str]
    ignored: int


class IncidentAlertOut(_Out):
    id: str
    service: str
    alertname: str
    severity: Severity
    source: str
    reason: str
    received_at: datetime


class SimilarIncidentOut(BaseModel):
    incident_id: str
    title: str
    at: datetime
    match: str  # "same cause" or "same service"
    cause: str
    cause_service: str
    statement: str
    fix: Optional[dict[str, Any]]
    outcome: Optional[str]
    fix_text: str


class MapServiceOut(BaseModel):
    name: str
    open_incident_id: Optional[str] = None
    open_incident_severity: Optional[Severity] = None


class MapEdgeOut(_Out):
    caller: str
    callee: str
    calls: int
    error_calls: int
    last_seen: datetime


class ServiceMapOut(BaseModel):
    services: list[MapServiceOut]
    edges: list[MapEdgeOut]
    updated_at: Optional[datetime]
    last_refresh: Optional[JobOut] = None


class StatsOut(BaseModel):
    incidents_by_status: dict[str, int]
    fixes_by_result: dict[str, int]  # verified, rolled_back, unverified, ... one count per finished or running fix
    investigations_done: int = 0
    median_investigation_seconds: Optional[float] = None  # over the last 500 investigations
    p95_investigation_seconds: Optional[float] = None
    ai_cost_usd_this_month: float = 0.0
    ai_investigations_this_month: int = 0


# --- a company's own connections --------------------------------------------------


class IntegrationKind(str, Enum):
    PROMETHEUS = "prometheus"
    LOKI = "loki"
    JAEGER = "jaeger"
    SERVICE_ADMIN = "service_admin"
    ARGOCD = "argocd"
    KUBERNETES = "kubernetes"


class IntegrationIn(_In):
    url: str = Field("", max_length=2000)
    endpoints: dict[str, Annotated[str, StringConstraints(max_length=2000)]] = Field(default_factory=dict,
                                                                                      max_length=100)
    username: Optional[str] = Field(None, max_length=200)
    token: Optional[str] = Field(None, max_length=4000)  # None keeps the saved one; "" removes it


class IntegrationOut(BaseModel):
    kind: IntegrationKind
    connected: bool
    url: str = ""
    endpoints: dict[str, str] = {}
    username: Optional[str] = None
    has_token: bool = False
    last_test_ok: Optional[bool] = None
    last_test_message: Optional[str] = None
    last_tested_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class RolesIn(_In):
    roles: list[Role] = Field(min_length=1)


# --- step 9: company sign-in ---------------------------------------------------------------

Domain = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=253,
                                          pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")]


class SsoConfigIn(_In):
    domain: Domain
    issuer: str = Field(min_length=8, max_length=2000)
    client_id: str = Field(min_length=1, max_length=500)
    client_secret: Optional[str] = Field(None, max_length=4000)  # None keeps the saved one
    default_role: Role = Role.VIEWER
    enforce: bool = False


class SsoConfigOut(BaseModel):
    configured: bool
    domain: str = ""
    issuer: str = ""
    client_id: str = ""
    has_secret: bool = False
    default_role: Role = Role.VIEWER
    enforce: bool = False
    redirect_uri: str = ""  # what the admin registers at the provider
    updated_at: Optional[datetime] = None


class SsoTestOut(BaseModel):
    ok: bool
    message: str
