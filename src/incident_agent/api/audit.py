"""Append-only audit trail."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from incident_agent.db.models import AuditEventRow


def record(
    db: Session,
    org_id: str,
    actor_id: Optional[str],
    event: str,
    object_type: str,
    object_id: str,
    details: Optional[dict[str, Any]] = None,
) -> None:
    """Add an audit event to the current transaction, so it commits with the change it describes."""
    db.add(
        AuditEventRow(
            org_id=org_id,
            actor_id=actor_id,
            event=event,
            object_type=object_type,
            object_id=object_id,
            details=details or {},
        )
    )
