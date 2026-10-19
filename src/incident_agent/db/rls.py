"""PostgreSQL row-level security: the database itself hides other companies' rows.

Each transaction runs with ``app.current_org`` set to the caller's organization
(see ``session.py``). Policies compare every row's ``org_id`` with it, so a
missing ``WHERE org_id = ...`` in application code still cannot leak data.

The app must connect as a role that is not a superuser, because superusers
bypass row-level security. ``FORCE`` makes the policies apply to the table
owner too.
"""

from __future__ import annotations

from sqlalchemy import Connection, text

from incident_agent.db.models import TENANT_TABLES

CURRENT_ORG = "current_setting('app.current_org', true)"
# Login must find a user by email before the organization is known. Only the
# login query turns this on, inside its own short transaction.
AUTH_LOOKUP = "current_setting('app.auth_lookup', true) = 'on'"
# Workers must find the next job before they know its organization. Only the
# claim query turns this on; the work itself runs as the job's organization.
WORKER_CLAIM = "current_setting('app.worker_claim', true) = 'on'"
CROSS_ORG_READ = {
    "users": AUTH_LOOKUP,
    "organizations": AUTH_LOOKUP,
    "alert_sources": AUTH_LOOKUP,  # an incoming alert is matched to its organization by token
    "sso_configs": AUTH_LOOKUP,  # sign-in finds the company by the email's domain
    "jobs": WORKER_CLAIM,
}


def _policy_sql(table: str) -> list[str]:
    key = "id" if table == "organizations" else "org_id"
    read = f"{key} = {CURRENT_ORG}"
    write = read
    if table in CROSS_ORG_READ:
        read = f"({read}) OR {CROSS_ORG_READ[table]}"
    if table == "jobs":
        write = read  # claiming a job updates it before its organization is known
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS tenant_isolation ON {table}",
        f"CREATE POLICY tenant_isolation ON {table} USING ({read}) WITH CHECK ({write})",
    ]


def apply_row_level_security(conn: Connection) -> None:
    if conn.dialect.name != "postgresql":
        return
    for table in TENANT_TABLES:
        for statement in _policy_sql(table):
            conn.execute(text(statement))
