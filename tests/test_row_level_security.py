"""The database itself must hide other companies' rows, even if app code forgets a filter."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from conftest import postgres_url, reset_postgres
from incident_agent.db.models import IncidentRow, OrganizationRow, TeamRow, UserRow
from incident_agent.db.session import init_db, make_engine, make_session_factory

pytestmark = pytest.mark.postgres


@pytest.fixture
def sessions():
    url = postgres_url()
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    reset_postgres(url)
    engine = make_engine(url)
    init_db(engine)
    factory = make_session_factory(engine)

    def open_as(org_id: str | None):
        s = factory()
        s.info["org_id"] = org_id
        return s

    for org in ("org_a", "org_b"):
        with open_as(org) as s:
            s.add(OrganizationRow(id=org, name=org))
            s.flush()
            s.add(TeamRow(id=f"team_{org}", org_id=org, name="default"))
            s.add(UserRow(id=f"usr_{org}", org_id=org, email=f"admin@{org}.com", password_hash="x", roles=["admin"]))
            s.flush()
            s.add(IncidentRow(
                org_id=org, team_id=f"team_{org}", title=f"{org} outage", service="api", environment="production",
                severity="sev1", status="open", started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                created_by=f"usr_{org}",
            ))
            s.commit()
    yield open_as
    engine.dispose()


def test_query_without_tenant_filter_only_sees_own_rows(sessions):
    with sessions("org_a") as s:
        titles = s.scalars(select(IncidentRow.title)).all()  # deliberately no WHERE org_id
    assert titles == ["org_a outage"]


def test_session_without_tenant_sees_nothing(sessions):
    with sessions(None) as s:
        assert s.scalar(select(func.count()).select_from(IncidentRow)) == 0
        assert s.scalar(select(func.count()).select_from(UserRow)) == 0


def test_cannot_write_rows_into_another_company(sessions):
    with sessions("org_a") as s:
        s.add(TeamRow(org_id="org_b", name="sneaky"))
        with pytest.raises(DBAPIError, match="row-level security"):
            s.flush()


def test_cannot_update_another_companys_rows(sessions):
    with sessions("org_a") as s:
        result = s.execute(text("UPDATE incidents SET status = 'closed' WHERE org_id = 'org_b'"))
        s.commit()
        assert result.rowcount == 0
    with sessions("org_b") as s:
        assert s.scalar(select(IncidentRow.status)) == "open"
