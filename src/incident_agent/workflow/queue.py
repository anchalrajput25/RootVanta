"""A job queue in the database, so any number of workers can share the work.

On PostgreSQL, ``FOR UPDATE SKIP LOCKED`` lets many workers claim jobs at the
same time without two of them getting the same one. A job whose worker died
is claimed again once its lease runs out; handlers must make that safe (see
``worker.py``: a fix is never applied twice).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from incident_agent.db.models import JobRow
from incident_agent.domain.models import utcnow

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
MAX_ATTEMPTS = 3
LEASE_DEFAULT = timedelta(minutes=10)
LEASE = {"investigate": LEASE_DEFAULT, "execute": timedelta(hours=2)}


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    org_id: str
    kind: str
    payload: dict[str, Any]
    attempts: int


def enqueue(db: Session, org_id: str, kind: str, payload: dict[str, Any]) -> JobRow:
    """Add a job to the caller's transaction; it becomes visible to workers on commit."""
    job = JobRow(org_id=org_id, kind=kind, payload=payload, status=QUEUED)
    db.add(job)
    db.flush()
    return job


def claim(factory: sessionmaker[Session], worker_id: str) -> Optional[ClaimedJob]:
    db = factory()
    db.info["worker_claim"] = True  # row-level security: the only cross-organization query
    try:
        now = utcnow()
        claimable = or_(JobRow.status == QUEUED, (JobRow.status == RUNNING) & (JobRow.lease_until < now))
        stmt = select(JobRow.id).where(claimable).order_by(JobRow.created_at).limit(1)
        if db.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        job_id = db.scalar(stmt)
        if job_id is None:
            db.rollback()
            return None
        # Conditional update: if another worker got there first, nothing changes.
        lease = now + LEASE_DEFAULT
        result = db.execute(
            update(JobRow).where(JobRow.id == job_id, claimable)
            .values(status=RUNNING, locked_by=worker_id, attempts=JobRow.attempts + 1, lease_until=lease)
        )
        if result.rowcount != 1:
            db.rollback()
            return None
        job = db.get(JobRow, job_id)
        job.lease_until = now + LEASE.get(job.kind, LEASE_DEFAULT)
        claimed = ClaimedJob(job.id, job.org_id, job.kind, dict(job.payload), job.attempts)
        db.commit()
        return claimed
    finally:
        db.close()


def finish(db: Session, job_id: str, error: Optional[str] = None) -> None:
    """Mark a job done (or failed) inside the job's organization session. The caller commits."""
    job = db.get(JobRow, job_id)
    if job is None:
        return
    job.status = FAILED if error else DONE
    job.error = error
    job.finished_at = utcnow()
    job.lease_until = None
