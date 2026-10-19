"""Database tables.

Every tenant-owned table has an ``org_id`` column. The API always filters by
it, and on PostgreSQL row-level security (see ``rls.py``) enforces it a second
time inside the database.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from incident_agent.domain.models import new_id, utcnow


class Base(DeclarativeBase):
    pass


def _id(prefix: str):
    return mapped_column(String(40), primary_key=True, default=lambda: new_id(prefix))


def _org_fk():
    return mapped_column(String(40), ForeignKey("organizations.id", ondelete="CASCADE"), index=True)


def _created():
    return mapped_column(DateTime(timezone=True), default=utcnow)


class OrganizationRow(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = _id("org")
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created()


class TeamRow(Base):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("org_id", "name"),)

    id: Mapped[str] = _id("team")
    org_id: Mapped[str] = _org_fk()
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created()


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = _id("usr")
    org_id: Mapped[str] = _org_fk()
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created()


class TeamMemberRow(Base):
    __tablename__ = "team_members"

    org_id: Mapped[str] = _org_fk()
    team_id: Mapped[str] = mapped_column(String(40), ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)


class IncidentRow(Base):
    __tablename__ = "incidents"

    id: Mapped[str] = _id("inc")
    org_id: Mapped[str] = _org_fk()
    team_id: Mapped[str] = mapped_column(String(40), ForeignKey("teams.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    service: Mapped[str] = mapped_column(String(200))
    environment: Mapped[str] = mapped_column(String(20))
    severity: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(40), ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created()


class RecoveryPlanRow(Base):
    __tablename__ = "recovery_plans"

    id: Mapped[str] = _id("plan")
    org_id: Mapped[str] = _org_fk()
    team_id: Mapped[str] = mapped_column(String(40), ForeignKey("teams.id"))
    incident_id: Mapped[str] = mapped_column(String(40), ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    action: Mapped[dict[str, Any]] = mapped_column(JSON)
    expected_effect: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(String(40), ForeignKey("users.id"))
    policy_decision: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = _created()


class ApprovalRow(Base):
    __tablename__ = "approvals"
    # One decision per approver per plan, so double clicks and races cannot
    # create duplicate approvals.
    __table_args__ = (UniqueConstraint("plan_id", "approver_id"),)

    id: Mapped[str] = _id("apr")
    org_id: Mapped[str] = _org_fk()
    plan_id: Mapped[str] = mapped_column(String(40), ForeignKey("recovery_plans.id", ondelete="CASCADE"), index=True)
    approver_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id"))
    decision: Mapped[str] = mapped_column(String(20))
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = _created()


class VerificationRow(Base):
    __tablename__ = "verifications"

    id: Mapped[str] = _id("ver")
    org_id: Mapped[str] = _org_fk()
    plan_id: Mapped[str] = mapped_column(String(40), ForeignKey("recovery_plans.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(30))
    verdict: Mapped[dict[str, Any]] = mapped_column(JSON)
    requested_by: Mapped[str] = mapped_column(String(40), ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created()


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = _id("aud")
    org_id: Mapped[str] = _org_fk()
    actor_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    event: Mapped[str] = mapped_column(String(100), index=True)
    object_type: Mapped[str] = mapped_column(String(50))
    object_id: Mapped[str] = mapped_column(String(40))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = _created()


class JobRow(Base):
    """Background work queue. Workers claim jobs across organizations (see rls.py),
    then do the work inside the job's own organization."""

    __tablename__ = "jobs"

    id: Mapped[str] = _id("job")
    org_id: Mapped[str] = _org_fk()
    kind: Mapped[str] = mapped_column(String(30))  # investigate, execute
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), index=True)  # queued, running, done, failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created()
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class InvestigationRow(Base):
    __tablename__ = "investigations"

    id: Mapped[str] = _id("inv")
    org_id: Mapped[str] = _org_fk()
    incident_id: Mapped[str] = mapped_column(String(40), ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[str] = mapped_column(String(40), ForeignKey("jobs.id"))
    status: Mapped[str] = mapped_column(String(20))  # queued, running, done, failed
    conclusion: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    report: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    report_markdown: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    plan_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    requested_by: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = _created()


class ExecutionRow(Base):
    __tablename__ = "executions"
    # A plan runs at most once, so a double click or a retried job cannot apply it twice.
    __table_args__ = (UniqueConstraint("plan_id"),)

    id: Mapped[str] = _id("exe")
    org_id: Mapped[str] = _org_fk()
    plan_id: Mapped[str] = mapped_column(String(40), ForeignKey("recovery_plans.id", ondelete="CASCADE"))
    job_id: Mapped[str] = mapped_column(String(40), ForeignKey("jobs.id"))
    # queued, running, verified, partially_verified, unverified, rolled_back,
    # apply_failed, undo_failed, interrupted
    status: Mapped[str] = mapped_column(String(30))
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    preview: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    requested_by: Mapped[str] = mapped_column(String(40), ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created()
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AlertSourceRow(Base):
    """A monitoring system allowed to open incidents, e.g. one Alertmanager.
    Only a hash of its token is stored."""

    __tablename__ = "alert_sources"

    id: Mapped[str] = _id("src")
    org_id: Mapped[str] = _org_fk()
    team_id: Mapped[str] = mapped_column(String(40), ForeignKey("teams.id"))
    name: Mapped[str] = mapped_column(String(200))
    environment: Mapped[str] = mapped_column(String(20))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_by: Mapped[str] = mapped_column(String(40), ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created()


class NotificationTargetRow(Base):
    """Where to send messages, e.g. a Slack incoming webhook."""

    __tablename__ = "notification_targets"

    id: Mapped[str] = _id("ntf")
    org_id: Mapped[str] = _org_fk()
    url: Mapped[str] = mapped_column(String(2000))
    created_by: Mapped[str] = mapped_column(String(40), ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created()


class IntegrationRow(Base):
    """A company's connection to its own monitoring or change system. The token is stored encrypted."""

    __tablename__ = "integrations"
    __table_args__ = (UniqueConstraint("org_id", "kind"),)

    id: Mapped[str] = _id("int")
    org_id: Mapped[str] = _org_fk()
    kind: Mapped[str] = mapped_column(String(30))
    url: Mapped[str] = mapped_column(String(2000), default="")
    endpoints: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # service -> URL, for the change system
    username: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    secret: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_test_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    last_tested_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[str] = mapped_column(String(40), ForeignKey("users.id"))
    updated_at: Mapped[datetime] = _created()


class InvestigationMetricRow(Base):
    """How long one finished investigation took and what its AI step cost.

    A separate table (not new columns on investigations) so existing
    databases pick it up without a migration.
    """

    __tablename__ = "investigation_metrics"

    id: Mapped[str] = _id("met")
    org_id: Mapped[str] = _org_fk()
    investigation_id: Mapped[str] = mapped_column(String(40), ForeignKey("investigations.id", ondelete="CASCADE"),
                                                  unique=True)
    conclusion: Mapped[str] = mapped_column(String(30))
    engine: Mapped[str] = mapped_column(String(80))
    duration_ms: Mapped[int] = mapped_column(Integer)
    ai_model: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = _created()


class SsoConfigRow(Base):
    """A company's single sign-on settings. People whose email is on ``domain`` sign in through
    the company's OpenID Connect provider. The client secret is stored encrypted."""

    __tablename__ = "sso_configs"

    id: Mapped[str] = _id("sso")
    org_id: Mapped[str] = mapped_column(String(40), ForeignKey("organizations.id", ondelete="CASCADE"), unique=True)
    domain: Mapped[str] = mapped_column(String(253), unique=True, index=True)
    issuer: Mapped[str] = mapped_column(String(2000))
    client_id: Mapped[str] = mapped_column(String(500))
    client_secret: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    default_role: Mapped[str] = mapped_column(String(20), default="viewer")
    enforce: Mapped[bool] = mapped_column(Boolean, default=False)  # no passwords, except for admins
    updated_by: Mapped[str] = mapped_column(String(40), ForeignKey("users.id"))
    updated_at: Mapped[datetime] = _created()


class IncidentAlertRow(Base):
    """One alert that opened an incident or was grouped into it."""

    __tablename__ = "incident_alerts"

    id: Mapped[str] = _id("alr")
    org_id: Mapped[str] = _org_fk()
    incident_id: Mapped[str] = mapped_column(String(40), ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    service: Mapped[str] = mapped_column(String(200))
    alertname: Mapped[str] = mapped_column(String(200))
    severity: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(300))  # why it joined this incident, or "opened the incident"
    received_at: Mapped[datetime] = _created()


class ServiceEdgeRow(Base):
    """One service calling another, learned from traces. Together they are the company's dependency map."""

    __tablename__ = "service_edges"
    __table_args__ = (UniqueConstraint("org_id", "caller", "callee"),)

    id: Mapped[str] = _id("edg")
    org_id: Mapped[str] = _org_fk()
    caller: Mapped[str] = mapped_column(String(200))
    callee: Mapped[str] = mapped_column(String(200))
    calls: Mapped[int] = mapped_column(Integer, default=0)  # calls seen in the latest sample
    error_calls: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = _created()
    last_seen: Mapped[datetime] = _created()


class IncidentMemoryRow(Base):
    """What an incident turned out to be and whether its fix worked, so later incidents can learn from it."""

    __tablename__ = "incident_memory"

    id: Mapped[str] = _id("mem")
    org_id: Mapped[str] = _org_fk()
    incident_id: Mapped[str] = mapped_column(String(40), ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    investigation_id: Mapped[str] = mapped_column(String(40), ForeignKey("investigations.id", ondelete="CASCADE"),
                                                  unique=True)
    service: Mapped[str] = mapped_column(String(200), index=True)  # the alerting service
    cause: Mapped[str] = mapped_column(String(40))
    cause_service: Mapped[str] = mapped_column(String(200))
    statement: Mapped[str] = mapped_column(String(500))
    fix: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)  # {type, service, params}
    plan_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    outcome: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)  # the fix's final status, once run
    created_at: Mapped[datetime] = _created()


TENANT_TABLES = (
    "organizations",
    "teams",
    "users",
    "team_members",
    "incidents",
    "recovery_plans",
    "approvals",
    "verifications",
    "audit_events",
    "jobs",
    "investigations",
    "executions",
    "alert_sources",
    "notification_targets",
    "integrations",
    "investigation_metrics",
    "sso_configs",
    "incident_alerts",
    "service_edges",
    "incident_memory",
)
