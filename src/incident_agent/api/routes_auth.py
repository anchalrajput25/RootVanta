"""Sign up a new organization, log in, and read the current user."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from incident_agent.api.audit import record
from incident_agent.api.deps import Caller, get_anonymous_db, get_caller
from incident_agent.api.routes_sso import password_login_blocked
from incident_agent.api.schemas import LoginIn, MeOut, SignupIn, TokenOut
from incident_agent.api.security import create_access_token, hash_password, verify_password
from incident_agent.db.models import OrganizationRow, TeamMemberRow, TeamRow, UserRow
from incident_agent.domain.models import Role, new_id

router = APIRouter(prefix="/auth", tags=["auth"])

# Checked even when the email is unknown, so response time does not reveal
# which emails have accounts.
_DUMMY_HASH = hash_password("not-a-real-password")


def _token(request: Request, user_id: str, org_id: str) -> TokenOut:
    settings = request.app.state.settings
    token = create_access_token(user_id, org_id, settings.jwt_secret, settings.token_ttl_minutes)
    return TokenOut(access_token=token, user_id=user_id, org_id=org_id)


@router.post("/signup", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
def signup(body: SignupIn, request: Request, db: Session = Depends(get_anonymous_db)) -> TokenOut:
    """Create a new organization with a default team and its first admin."""
    org_id = new_id("org")
    db.info["org_id"] = org_id  # row-level security: we act inside the new org
    try:
        org = OrganizationRow(id=org_id, name=body.organization_name)
        team = TeamRow(org_id=org_id, name="default")
        admin = UserRow(
            org_id=org_id,
            email=body.email,
            password_hash=hash_password(body.password),
            roles=[Role.ADMIN.value],
        )
        db.add(org)
        db.flush()
        db.add_all([team, admin])
        db.flush()
        db.add(TeamMemberRow(org_id=org_id, team_id=team.id, user_id=admin.id))
        record(db, org_id, admin.id, "organization.created", "organization", org_id, {"name": org.name})
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "an account with this email already exists") from None
    return _token(request, admin.id, org_id)


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, db: Session = Depends(get_anonymous_db)) -> TokenOut:
    db.info["auth_lookup"] = True  # the only query allowed to search users across orgs
    row = db.execute(
        select(UserRow.id, UserRow.org_id, UserRow.password_hash, UserRow.is_active, UserRow.roles)
        .where(UserRow.email == body.email)
    ).first()
    blocked = row is not None and password_login_blocked(db, row.org_id, row.roles)
    # End the lookup transaction before acting as the user's org. Plain values,
    # not ORM objects, so nothing lazily reloads inside the wrong transaction.
    db.rollback()
    db.info["auth_lookup"] = False
    if row is None:
        verify_password(body.password, _DUMMY_HASH)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong email or password")
    user_id, org_id, password_hash, is_active, _ = row
    if not verify_password(body.password, password_hash) or not is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong email or password")
    if blocked:  # told only after the right password, so it reveals nothing to a guesser
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Your company signs in with its own account. Use 'Sign in with your company account'.")

    db.info["org_id"] = org_id
    record(db, org_id, user_id, "user.logged_in", "user", user_id)
    db.commit()
    return _token(request, user_id, org_id)


@router.get("/me", response_model=MeOut)
def me(caller: Caller = Depends(get_caller)) -> MeOut:
    u = caller.user
    return MeOut(id=u.id, org_id=u.org_id, email=u.email, roles=sorted(u.roles), team_ids=sorted(u.team_ids))
