"""Settings > Monitoring: each company connects its own Prometheus, Loki, Jaeger and change system."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select

from incident_agent.api.audit import record
from incident_agent.api.deps import Caller, get_caller
from incident_agent.api.schemas import IntegrationIn, IntegrationKind, IntegrationOut
from incident_agent.db.models import IntegrationRow
from incident_agent.domain.models import Role, utcnow
from incident_agent.integrations import probe
from incident_agent.integrations.store import check_url
from incident_agent.remediation.argocd import check_app_name
from incident_agent.remediation.executor import ExecutorError
from incident_agent.remediation.kubernetes import split_target

router = APIRouter(tags=["integrations"])


def _out(row: IntegrationRow | None, kind: IntegrationKind) -> IntegrationOut:
    if row is None:
        return IntegrationOut(kind=kind, connected=False)
    return IntegrationOut(kind=kind, connected=True, url=row.url, endpoints=dict(row.endpoints or {}),
                          username=row.username, has_token=bool(row.secret), last_test_ok=row.last_test_ok,
                          last_test_message=row.last_test_message, last_tested_at=row.last_tested_at,
                          updated_at=row.updated_at)


def _row(caller: Caller, kind: IntegrationKind) -> IntegrationRow | None:
    return caller.db.scalar(select(IntegrationRow).where(IntegrationRow.org_id == caller.org_id,
                                                         IntegrationRow.kind == kind.value))


@router.get("/integrations", response_model=list[IntegrationOut])
def list_integrations(caller: Caller = Depends(get_caller)) -> list[IntegrationOut]:
    caller.require(Role.ADMIN)
    return [_out(_row(caller, kind), kind) for kind in IntegrationKind]


@router.put("/integrations/{kind}", response_model=IntegrationOut)
def save_integration(kind: IntegrationKind, body: IntegrationIn, request: Request,
                     caller: Caller = Depends(get_caller)) -> IntegrationOut:
    caller.require(Role.ADMIN)
    endpoints = {k.strip(): v.strip() for k, v in body.endpoints.items() if k.strip() and v.strip()}
    url = body.url.strip()
    try:
        if kind == IntegrationKind.SERVICE_ADMIN:
            if not endpoints:
                raise ValueError("list at least one service and its admin address")
            for address in endpoints.values():
                check_url(address)
            url = ""
        else:
            check_url(url)
        if kind == IntegrationKind.ARGOCD:
            for app in endpoints.values():
                check_app_name(app)
        if kind == IntegrationKind.KUBERNETES:
            if not endpoints:
                raise ValueError("list at least one service and its Deployment, written namespace/name")
            for target in endpoints.values():
                split_target(target)
    except (ValueError, ExecutorError) as e:
        raise HTTPException(422, str(e)) from None

    row = _row(caller, kind)
    if row is None:
        row = IntegrationRow(org_id=caller.org_id, kind=kind.value, updated_by=caller.user.id)
        caller.db.add(row)
    row.url, row.endpoints = url, endpoints
    row.username = (body.username or "").strip() or None
    if body.token is not None:
        row.secret = request.app.state.secret_box.seal(body.token) if body.token else None
    row.updated_by, row.updated_at = caller.user.id, utcnow()
    row.last_test_ok = row.last_test_message = row.last_tested_at = None  # settings changed: test again
    caller.db.flush()
    # Never the token itself: the audit log is readable by every admin.
    record(caller.db, caller.org_id, caller.user.id, "integration.saved", "integration", row.id,
           {"kind": kind.value, "url": url, "services": sorted(endpoints), "has_token": bool(row.secret)})
    caller.db.commit()
    return _out(row, kind)


@router.delete("/integrations/{kind}", status_code=status.HTTP_204_NO_CONTENT)
def delete_integration(kind: IntegrationKind, caller: Caller = Depends(get_caller)) -> None:
    caller.require(Role.ADMIN)
    row = _row(caller, kind)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "integration not found")
    caller.db.delete(row)
    record(caller.db, caller.org_id, caller.user.id, "integration.removed", "integration", row.id,
           {"kind": kind.value})
    caller.db.commit()


@router.post("/integrations/{kind}/test", response_model=IntegrationOut)
def test_integration(kind: IntegrationKind, request: Request, caller: Caller = Depends(get_caller)) -> IntegrationOut:
    caller.require(Role.ADMIN)
    row = _row(caller, kind)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "save the connection first")
    try:
        ok, message = probe.test(request.app.state.connections.to_integration(row))
    except ValueError as e:  # the saved token cannot be decrypted
        ok, message = False, str(e)
    row.last_test_ok, row.last_test_message, row.last_tested_at = ok, message[:500], utcnow()
    record(caller.db, caller.org_id, caller.user.id, "integration.tested", "integration", row.id,
           {"kind": kind.value, "ok": ok})
    caller.db.commit()
    return _out(row, kind)
