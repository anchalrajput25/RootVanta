"""Request-scoped dependencies: database session and the signed-in user."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, TypeVar

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from incident_agent.api.security import decode_access_token
from incident_agent.db.models import Base, TeamMemberRow, UserRow
from incident_agent.domain.models import Role, User

_bearer = HTTPBearer(auto_error=False)
M = TypeVar("M", bound=Base)


@dataclass
class Caller:
    """The authenticated user plus a session scoped to their organization."""

    user: User
    db: Session

    @property
    def org_id(self) -> str:
        return self.user.org_id

    def require(self, *roles: Role) -> None:
        if not self.user.has_role(*roles):
            names = ", ".join(r.value for r in roles)
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role: {names}")

    def get(self, model: type[M], object_id: str) -> M:
        """Load a row of the caller's organization, or 404 (never 403, so ids don't leak)."""
        row = self.db.get(model, object_id)
        if row is None or getattr(row, "org_id", None) != self.org_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{model.__tablename__[:-1]} not found")
        return row


def get_anonymous_db(request: Request) -> Iterator[Session]:
    db = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


def load_domain_user(db: Session, row: UserRow) -> User:
    team_ids = db.scalars(
        select(TeamMemberRow.team_id).where(TeamMemberRow.user_id == row.id, TeamMemberRow.org_id == row.org_id)
    ).all()
    return User(
        id=row.id,
        org_id=row.org_id,
        email=row.email,
        team_ids=frozenset(team_ids),
        roles=frozenset(Role(r) for r in row.roles),
    )


def get_caller(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Iterator[Caller]:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED, "invalid or missing token", headers={"WWW-Authenticate": "Bearer"}
    )
    if credentials is None:
        raise unauthorized
    try:
        claims = decode_access_token(credentials.credentials, request.app.state.settings.jwt_secret)
    except jwt.PyJWTError:
        raise unauthorized from None

    db: Session = request.app.state.session_factory()
    db.info["org_id"] = claims["org"]
    try:
        # Roles and membership are read fresh on every request, so removing a
        # role or deactivating a user takes effect immediately. One query for
        # both: this runs on every request, so it is kept cheap.
        rows = db.execute(
            select(UserRow.org_id, UserRow.email, UserRow.roles, UserRow.is_active, TeamMemberRow.team_id)
            .outerjoin(TeamMemberRow, (TeamMemberRow.user_id == UserRow.id) & (TeamMemberRow.org_id == UserRow.org_id))
            .where(UserRow.id == claims["sub"])
        ).all()
        if not rows or not rows[0].is_active or rows[0].org_id != claims["org"]:
            raise unauthorized
        first = rows[0]
        user = User(id=claims["sub"], org_id=first.org_id, email=first.email,
                    team_ids=frozenset(r.team_id for r in rows if r.team_id),
                    roles=frozenset(Role(r) for r in first.roles))
        yield Caller(user=user, db=db)
    finally:
        db.close()
