"""Teams, users and the audit log of the caller's organization."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from incident_agent.api.audit import record
from incident_agent.api.deps import Caller, get_caller, load_domain_user
from incident_agent.api.schemas import AuditOut, RolesIn, TeamIn, TeamOut, UserIn, UserOut
from incident_agent.api.security import hash_password
from incident_agent.db.models import AuditEventRow, TeamMemberRow, TeamRow, UserRow
from incident_agent.domain.models import Role

router = APIRouter(tags=["organization"])


@router.get("/teams", response_model=list[TeamOut])
def list_teams(caller: Caller = Depends(get_caller)) -> list[TeamRow]:
    return list(caller.db.scalars(select(TeamRow).where(TeamRow.org_id == caller.org_id).order_by(TeamRow.name)))


@router.post("/teams", response_model=TeamOut, status_code=status.HTTP_201_CREATED)
def create_team(body: TeamIn, caller: Caller = Depends(get_caller)) -> TeamRow:
    caller.require(Role.ADMIN)
    team = TeamRow(org_id=caller.org_id, name=body.name)
    caller.db.add(team)
    try:
        caller.db.flush()
    except IntegrityError:
        caller.db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "a team with this name already exists") from None
    record(caller.db, caller.org_id, caller.user.id, "team.created", "team", team.id, {"name": team.name})
    caller.db.commit()
    return team


def _user_out(caller: Caller, row: UserRow) -> UserOut:
    u = load_domain_user(caller.db, row)
    return UserOut(id=u.id, email=u.email, roles=sorted(u.roles), team_ids=sorted(u.team_ids), is_active=row.is_active)


@router.get("/users", response_model=list[UserOut])
def list_users(caller: Caller = Depends(get_caller)) -> list[UserOut]:
    caller.require(Role.ADMIN)
    rows = caller.db.scalars(select(UserRow).where(UserRow.org_id == caller.org_id).order_by(UserRow.email))
    return [_user_out(caller, r) for r in rows]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: UserIn, caller: Caller = Depends(get_caller)) -> UserOut:
    """Admins add people to their own organization and choose their roles and teams."""
    caller.require(Role.ADMIN)
    for team_id in body.team_ids:
        caller.get(TeamRow, team_id)  # 404 unless the team is in this org
    row = UserRow(
        org_id=caller.org_id,
        email=body.email,
        password_hash=hash_password(body.password),
        roles=sorted({r.value for r in body.roles}),
    )
    caller.db.add(row)
    try:
        caller.db.flush()
    except IntegrityError:
        caller.db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "an account with this email already exists") from None
    for team_id in set(body.team_ids):
        caller.db.add(TeamMemberRow(org_id=caller.org_id, team_id=team_id, user_id=row.id))
    record(
        caller.db, caller.org_id, caller.user.id, "user.created", "user", row.id,
        {"email": row.email, "roles": row.roles, "team_ids": sorted(set(body.team_ids))},
    )
    caller.db.commit()
    return _user_out(caller, row)


@router.post("/users/{user_id}/deactivate", response_model=UserOut)
def deactivate_user(user_id: str, caller: Caller = Depends(get_caller)) -> UserOut:
    caller.require(Role.ADMIN)
    row = caller.get(UserRow, user_id)
    if row.id == caller.user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "you cannot deactivate yourself")
    row.is_active = False
    record(caller.db, caller.org_id, caller.user.id, "user.deactivated", "user", row.id)
    caller.db.commit()
    return _user_out(caller, row)


@router.post("/users/{user_id}/activate", response_model=UserOut)
def activate_user(user_id: str, caller: Caller = Depends(get_caller)) -> UserOut:
    caller.require(Role.ADMIN)
    row = caller.get(UserRow, user_id)
    row.is_active = True
    record(caller.db, caller.org_id, caller.user.id, "user.activated", "user", row.id)
    caller.db.commit()
    return _user_out(caller, row)


@router.put("/users/{user_id}/roles", response_model=UserOut)
def set_roles(user_id: str, body: RolesIn, caller: Caller = Depends(get_caller)) -> UserOut:
    caller.require(Role.ADMIN)
    row = caller.get(UserRow, user_id)
    roles = sorted({r.value for r in body.roles})
    if row.id == caller.user.id and Role.ADMIN.value not in roles:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "you cannot remove your own admin role")
    before, row.roles = row.roles, roles
    record(caller.db, caller.org_id, caller.user.id, "user.roles_changed", "user", row.id,
           {"before": before, "after": roles})
    caller.db.commit()
    return _user_out(caller, row)


@router.get("/audit", response_model=list[AuditOut])
def audit_log(
    caller: Caller = Depends(get_caller),
    limit: int = Query(100, ge=1, le=1000),
) -> list[AuditEventRow]:
    caller.require(Role.ADMIN)
    stmt = (
        select(AuditEventRow)
        .where(AuditEventRow.org_id == caller.org_id)
        .order_by(AuditEventRow.at.desc(), AuditEventRow.id.desc())
        .limit(limit)
    )
    return list(caller.db.scalars(stmt))
