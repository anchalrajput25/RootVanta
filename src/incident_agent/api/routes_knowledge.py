"""What the agent has learned: the alerts in each incident, similar past incidents, and the dependency map."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from incident_agent.api.audit import record
from incident_agent.api.deps import Caller, get_caller
from incident_agent.api.routes_workflow import OPEN_STATES
from incident_agent.api.schemas import (
    IncidentAlertOut,
    JobOut,
    MapEdgeOut,
    MapServiceOut,
    ServiceMapOut,
    SimilarIncidentOut,
)
from incident_agent.db.models import IncidentAlertRow, IncidentMemoryRow, IncidentRow, JobRow, ServiceEdgeRow
from incident_agent.domain.models import Role
from incident_agent.knowledge import memory, service_map
from incident_agent.workflow import queue

router = APIRouter(tags=["knowledge"])


@router.get("/incidents/{incident_id}/alerts", response_model=list[IncidentAlertOut])
def incident_alerts(incident_id: str, caller: Caller = Depends(get_caller)) -> list[IncidentAlertRow]:
    incident = caller.get(IncidentRow, incident_id)
    return list(caller.db.scalars(select(IncidentAlertRow).where(IncidentAlertRow.org_id == caller.org_id,
                                                                 IncidentAlertRow.incident_id == incident.id)
                                  .order_by(IncidentAlertRow.received_at)))


@router.get("/incidents/{incident_id}/similar", response_model=list[SimilarIncidentOut])
def similar_incidents(incident_id: str, caller: Caller = Depends(get_caller)) -> list[SimilarIncidentOut]:
    """Earlier incidents with the same cause (once this one's cause is known), or on the same service."""
    incident = caller.get(IncidentRow, incident_id)
    mine = caller.db.scalar(select(IncidentMemoryRow).where(IncidentMemoryRow.org_id == caller.org_id,
                                                            IncidentMemoryRow.incident_id == incident.id)
                            .order_by(IncidentMemoryRow.created_at.desc()))
    found = memory.similar(caller.db, caller.org_id, incident.service, mine.cause if mine else None,
                           mine.cause_service if mine else None, exclude_incident=incident.id)
    return [SimilarIncidentOut(incident_id=s.incident_id, title=s.title, at=s.at, match=s.match, cause=s.cause,
                               cause_service=s.cause_service, statement=s.statement, fix=s.fix, outcome=s.outcome,
                               fix_text=s.fix_text) for s in found]


def _last_refresh(caller: Caller) -> Optional[JobRow]:
    return caller.db.scalar(select(JobRow).where(JobRow.org_id == caller.org_id, JobRow.kind == "map")
                            .order_by(JobRow.created_at.desc()))


@router.get("/service-map", response_model=ServiceMapOut)
def get_service_map(caller: Caller = Depends(get_caller)) -> ServiceMapOut:
    edges = service_map.edges(caller.db, caller.org_id)
    names = sorted({e.caller for e in edges} | {e.callee for e in edges})
    incidents = caller.db.execute(
        select(IncidentRow.service, IncidentRow.id, IncidentRow.severity)
        .where(IncidentRow.org_id == caller.org_id, IncidentRow.status.in_(OPEN_STATES))
        .order_by(IncidentRow.severity.desc(), IncidentRow.created_at)).all()
    worst = {service: (inc_id, sev) for service, inc_id, sev in incidents}  # most severe last, so it wins
    services = [MapServiceOut(name=n, open_incident_id=worst.get(n, (None, None))[0],
                              open_incident_severity=worst.get(n, (None, None))[1]) for n in names]
    # Services with an open incident but not (yet) on the map are still shown.
    services += [MapServiceOut(name=n, open_incident_id=i, open_incident_severity=sev)
                 for n, (i, sev) in worst.items() if n not in names]
    updated = caller.db.scalar(select(func.max(ServiceEdgeRow.last_seen))
                               .where(ServiceEdgeRow.org_id == caller.org_id))
    last = _last_refresh(caller)
    return ServiceMapOut(services=services, edges=[MapEdgeOut.model_validate(e) for e in edges], updated_at=updated,
                         last_refresh=JobOut.model_validate(last) if last else None)


@router.post("/service-map/refresh", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def refresh_service_map(caller: Caller = Depends(get_caller)) -> JobRow:
    """Queue a scan of recent traces of every service. Needs Jaeger connected."""
    caller.require(Role.RESPONDER, Role.APPROVER, Role.ADMIN)
    last = _last_refresh(caller)
    if last is not None and last.status in (queue.QUEUED, queue.RUNNING):
        raise HTTPException(status.HTTP_409_CONFLICT, "the map is already being updated")
    job = queue.enqueue(caller.db, caller.org_id, "map", {"requested_by": caller.user.id})
    record(caller.db, caller.org_id, caller.user.id, "service_map.refresh_queued", "job", job.id)
    caller.db.commit()
    return job
