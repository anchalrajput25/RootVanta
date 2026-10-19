"""Engine and per-request sessions bound to one organization."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from incident_agent.db.models import Base
from incident_agent.db.rls import apply_row_level_security


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        if url in ("sqlite://", "sqlite:///:memory:"):
            # Tests: one shared in-memory connection, or each thread would see an empty database.
            return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
        # A database file: one connection per request, so requests that run at the same time
        # (the dashboard sends several) never share a transaction. Writers wait for each other.
        return create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20)


# Any fixed number; every API process uses the same one.
_SCHEMA_LOCK_ID = 7_340_001


def init_db(engine: Engine) -> None:
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            # Several API processes start at once; let one create the schema at a time.
            conn.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _SCHEMA_LOCK_ID})
        Base.metadata.create_all(conn)
        apply_row_level_security(conn)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @event.listens_for(factory, "after_begin")
    def _set_tenant(session: Session, transaction, connection) -> None:  # noqa: ANN001
        # Transaction-local settings, so a pooled connection never carries
        # one company's context into another request.
        if connection.dialect.name != "postgresql":
            return
        org_id: Optional[str] = session.info.get("org_id")
        connection.execute(text("SELECT set_config('app.current_org', :org, true)"), {"org": org_id or ""})
        if session.info.get("auth_lookup"):
            connection.execute(text("SELECT set_config('app.auth_lookup', 'on', true)"))
        if session.info.get("worker_claim"):
            connection.execute(text("SELECT set_config('app.worker_claim', 'on', true)"))

    return factory
