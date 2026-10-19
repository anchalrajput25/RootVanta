"""Each company's dependency map: which service calls which, learned from its traces.

Investigations add what their traces show, and a "map" job scans every service
in Jaeger. The map is used to group alerts that belong to one outage, to know a
service's dependencies when the incident window has no traces, and is shown in
the dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from incident_agent.db.models import ServiceEdgeRow
from incident_agent.domain.models import utcnow
from incident_agent.investigation.collectors import service_calls
from incident_agent.investigation.models import ServiceCall
from incident_agent.telemetry.jaeger import JaegerClient

# An edge not seen for this long is treated as gone (the service stopped calling it).
MAX_AGE = timedelta(days=14)
MAP_LOOKBACK = timedelta(hours=1)
TRACES_PER_SERVICE = 100
MAX_SERVICES = 200


def save(db: Session, org_id: str, calls: Iterable[ServiceCall], seen_at: datetime | None = None) -> int:
    """Add or refresh edges. Returns how many were new."""
    seen_at = seen_at or utcnow()
    new = 0
    for call in calls:
        caller, callee = call.caller[:200], call.callee[:200]
        row = db.scalar(select(ServiceEdgeRow).where(ServiceEdgeRow.org_id == org_id,
                                                     ServiceEdgeRow.caller == caller,
                                                     ServiceEdgeRow.callee == callee))
        if row is None:
            try:
                with db.begin_nested():  # another worker may add the same edge at the same moment
                    db.add(ServiceEdgeRow(org_id=org_id, caller=caller, callee=callee, calls=call.calls,
                                          error_calls=call.errors, first_seen=seen_at, last_seen=seen_at))
                new += 1
                continue
            except IntegrityError:
                row = db.scalar(select(ServiceEdgeRow).where(ServiceEdgeRow.org_id == org_id,
                                                             ServiceEdgeRow.caller == caller,
                                                             ServiceEdgeRow.callee == callee))
        row.calls, row.error_calls = call.calls, call.errors
        row.last_seen = max(_aware(row.last_seen), seen_at)
    return new


def _aware(t: datetime) -> datetime:
    """SQLite gives back naive datetimes; they are stored as UTC."""
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _fresh(org_id: str):
    return (ServiceEdgeRow.org_id == org_id) & (ServiceEdgeRow.last_seen >= utcnow() - MAX_AGE)


def edges(db: Session, org_id: str) -> list[ServiceEdgeRow]:
    return list(db.scalars(select(ServiceEdgeRow).where(_fresh(org_id))
                           .order_by(ServiceEdgeRow.caller, ServiceEdgeRow.callee)))


@dataclass(frozen=True)
class Neighbours:
    callees: frozenset[str]  # services this one calls
    callers: frozenset[str]  # services that call this one


def neighbours(db: Session, org_id: str, service: str) -> Neighbours:
    rows = db.execute(select(ServiceEdgeRow.caller, ServiceEdgeRow.callee).where(
        _fresh(org_id), or_(ServiceEdgeRow.caller == service, ServiceEdgeRow.callee == service))).all()
    return Neighbours(callees=frozenset(b for a, b in rows if a == service),
                      callers=frozenset(a for a, b in rows if b == service))


def callees_reader(factory: sessionmaker[Session], org_id: str):
    """For Collectors.known_callees: reads the saved map in its own short session."""

    def read(service: str) -> list[str]:
        with factory() as db:
            db.info["org_id"] = org_id
            return sorted(neighbours(db, org_id, service).callees)

    return read


@dataclass(frozen=True)
class MapScan:
    services: list[str]
    calls: list[ServiceCall]
    failed: list[str]  # services whose traces could not be read


def scan(jaeger: JaegerClient, end: datetime | None = None) -> MapScan:
    """Read recent traces of every service Jaeger knows and collect who calls whom."""
    end = end or utcnow()
    start = end - MAP_LOOKBACK
    services = jaeger.services()[:MAX_SERVICES]
    merged: dict[tuple[str, str], ServiceCall] = {}
    failed: list[str] = []
    for service in services:
        try:
            traces = jaeger.traces(service, start, end, limit=TRACES_PER_SERVICE)
        except Exception:  # noqa: BLE001 - one unreadable service must not lose the rest of the map
            failed.append(service)
            continue
        for c in service_calls(traces):
            key = (c.caller, c.callee)
            seen = merged.get(key)
            # The same trace is found from each service in it, so keep the largest count, not the sum.
            if seen is None or c.calls > seen.calls:
                merged[key] = c
    return MapScan(services=services, calls=sorted(merged.values(), key=lambda c: (c.caller, c.callee)),
                   failed=failed)
