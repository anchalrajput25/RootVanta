"""Company sign-in: settings for admins, and the browser redirects to and from the provider."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any, Optional
from urllib.parse import quote

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from incident_agent.api.audit import record
from incident_agent.api.deps import Caller, get_anonymous_db, get_caller
from incident_agent.api.schemas import SsoConfigIn, SsoConfigOut, SsoTestOut
from incident_agent.api.security import create_access_token
from incident_agent.auth.oidc import OidcClient, Pending, Provider, SsoError, email_of, new_pending
from incident_agent.db.models import SsoConfigRow, TeamMemberRow, TeamRow, UserRow
from incident_agent.domain.models import Role, utcnow
from incident_agent.integrations.store import check_url

router = APIRouter(tags=["sso"])

COOKIE = "ia_sso"
COOKIE_PATH = "/api/v1/auth/sso"
COOKIE_AUDIENCE = "incident-agent-sso"
SIGN_IN_MINUTES = 10
# Accounts made by single sign-on have no password. This value is not a valid
# hash, so a password login can never match it.
NO_PASSWORD = "sso-only"


def _oidc(request: Request) -> OidcClient:
    if not hasattr(request.app.state, "oidc"):
        request.app.state.oidc = OidcClient()
    return request.app.state.oidc


def _redirect_uri(request: Request) -> str:
    return f"{request.app.state.settings.public_url}/api/v1/auth/sso/callback"


def _back(request: Request, **fragment: str) -> RedirectResponse:
    """Back to the dashboard. The result goes in the URL fragment, which browsers never send to servers."""
    frag = "&".join(f"{k}={quote(v)}" for k, v in fragment.items())
    r = RedirectResponse(f"{request.app.state.settings.public_url}/#{frag}", status_code=status.HTTP_303_SEE_OTHER)
    r.delete_cookie(COOKIE, path=COOKIE_PATH)
    return r


def _out(request: Request, row: Optional[SsoConfigRow]) -> SsoConfigOut:
    if row is None:
        return SsoConfigOut(configured=False, redirect_uri=_redirect_uri(request))
    return SsoConfigOut(configured=True, domain=row.domain, issuer=row.issuer, client_id=row.client_id,
                        has_secret=bool(row.client_secret), default_role=Role(row.default_role), enforce=row.enforce,
                        redirect_uri=_redirect_uri(request), updated_at=row.updated_at)


def _row(caller: Caller) -> Optional[SsoConfigRow]:
    return caller.db.scalar(select(SsoConfigRow).where(SsoConfigRow.org_id == caller.org_id))


def _provider(request: Request, row: SsoConfigRow) -> Provider:
    secret = request.app.state.secret_box.open(row.client_secret) if row.client_secret else ""
    return Provider(issuer=row.issuer, client_id=row.client_id, client_secret=secret)


# --- settings, for admins --------------------------------------------------------------------


@router.get("/sso", response_model=SsoConfigOut)
def get_sso(request: Request, caller: Caller = Depends(get_caller)) -> SsoConfigOut:
    caller.require(Role.ADMIN)
    return _out(request, _row(caller))


@router.put("/sso", response_model=SsoConfigOut)
def save_sso(body: SsoConfigIn, request: Request, caller: Caller = Depends(get_caller)) -> SsoConfigOut:
    caller.require(Role.ADMIN)
    issuer = body.issuer.strip().rstrip("/")
    try:
        check_url(issuer)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    if not issuer.startswith("https://") and not issuer.startswith("http://localhost"):
        raise HTTPException(422, "the issuer address must start with https://")
    row = _row(caller)
    if not body.client_secret and (row is None or not row.client_secret):
        raise HTTPException(422, "enter the client secret")
    if row is None:
        row = SsoConfigRow(org_id=caller.org_id, updated_by=caller.user.id)
        caller.db.add(row)
    row.domain, row.issuer, row.client_id = body.domain, issuer, body.client_id.strip()
    if body.client_secret:
        row.client_secret = request.app.state.secret_box.seal(body.client_secret)
    row.default_role, row.enforce = body.default_role.value, body.enforce
    row.updated_by, row.updated_at = caller.user.id, utcnow()
    try:
        caller.db.flush()
    except IntegrityError:
        caller.db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"another company already signs in people from {body.domain}") \
            from None
    record(caller.db, caller.org_id, caller.user.id, "sso.saved", "sso_config", row.id,
           {"domain": row.domain, "issuer": row.issuer, "default_role": row.default_role, "enforce": row.enforce})
    caller.db.commit()
    return _out(request, row)


@router.delete("/sso", status_code=status.HTTP_204_NO_CONTENT)
def delete_sso(caller: Caller = Depends(get_caller)) -> None:
    caller.require(Role.ADMIN)
    row = _row(caller)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "company sign-in is not set up")
    caller.db.delete(row)
    record(caller.db, caller.org_id, caller.user.id, "sso.removed", "sso_config", row.id, {"domain": row.domain})
    caller.db.commit()


@router.post("/sso/test", response_model=SsoTestOut)
def test_sso(request: Request, caller: Caller = Depends(get_caller)) -> SsoTestOut:
    """Reads the provider's published settings. A real sign-in is the final test."""
    caller.require(Role.ADMIN)
    row = _row(caller)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "save the settings first")
    try:
        doc = _oidc(request).discover(row.issuer)
    except SsoError as e:
        return SsoTestOut(ok=False, message=str(e))
    return SsoTestOut(ok=True, message=f"Found the provider at {doc['issuer']}. Now try signing in with a "
                                       f"{row.domain} account in a private window.")


# --- sign-in -----------------------------------------------------------------------------------


def find_config(db: Session, domain: str) -> Optional[tuple[str, SsoConfigRow]]:
    """The company that signs in people from ``domain``. Runs before the company is known."""
    db.info["auth_lookup"] = True
    try:
        row = db.scalar(select(SsoConfigRow).where(SsoConfigRow.domain == domain))
        if row is None:
            return None
        db.expunge(row)  # plain values only: nothing may reload inside another company's transaction
        return row.org_id, row
    finally:
        db.rollback()
        db.info["auth_lookup"] = False


@router.get("/auth/sso/start")
def sso_start(email: str, request: Request, db: Session = Depends(get_anonymous_db)) -> RedirectResponse:
    email = email.strip().lower()
    domain = email.rsplit("@", 1)[1] if "@" in email else email
    found = find_config(db, domain)
    if found is None:
        return _back(request, sso_error=f"Company sign-in is not set up for {domain}. Log in with your password, "
                                        "or ask your admin.")
    org_id, row = found
    pending = new_pending()
    try:
        url = _oidc(request).authorize_url(_provider(request, row), _redirect_uri(request), pending,
                                           login_hint=email if "@" in email else "")
    except (SsoError, ValueError) as e:
        return _back(request, sso_error=str(e))
    settings = request.app.state.settings
    cookie = jwt.encode({"org": org_id, "state": pending.state, "nonce": pending.nonce, "verifier": pending.verifier,
                         "aud": COOKIE_AUDIENCE, "exp": utcnow() + timedelta(minutes=SIGN_IN_MINUTES)},
                        settings.jwt_secret, algorithm="HS256")
    r = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    r.set_cookie(COOKIE, cookie, max_age=SIGN_IN_MINUTES * 60, path=COOKIE_PATH, httponly=True, samesite="lax",
                 secure=settings.public_url.startswith("https://"))
    return r


def _read_cookie(request: Request) -> tuple[str, Pending]:
    raw = request.cookies.get(COOKIE)
    if not raw:
        raise SsoError("The sign-in took too long or was started in another browser. Please try again.")
    try:
        c: dict[str, Any] = jwt.decode(raw, request.app.state.settings.jwt_secret, algorithms=["HS256"],
                                       audience=COOKIE_AUDIENCE)
    except jwt.PyJWTError:
        raise SsoError("The sign-in took too long. Please try again.") from None
    return c["org"], Pending(state=c["state"], nonce=c["nonce"], verifier=c["verifier"])


@router.get("/auth/sso/callback")
def sso_callback(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = "",
                 db: Session = Depends(get_anonymous_db)) -> RedirectResponse:
    try:
        if error:
            raise SsoError(f"The sign-in provider said: {error_description or error}")
        org_id, pending = _read_cookie(request)
        if not state or not secrets.compare_digest(state, pending.state):
            raise SsoError("This sign-in does not match the one started here. Please try again.")
        db.info["org_id"] = org_id
        row = db.scalar(select(SsoConfigRow).where(SsoConfigRow.org_id == org_id))
        if row is None:
            raise SsoError("Company sign-in was turned off. Ask your admin.")
        claims = _oidc(request).finish(_provider(request, row), _redirect_uri(request), code, pending)
        email = email_of(claims)
        if email is None:
            raise SsoError("The sign-in provider did not share an email address. Ask your admin to allow the "
                           "'email' scope.")
        if email.rsplit("@", 1)[1] != row.domain:
            raise SsoError(f"{email} is not a {row.domain} address.")
        user_id = _user_for(db, org_id, email, row)
        record(db, org_id, user_id, "user.logged_in", "user", user_id, {"method": "sso"})
        db.commit()
    except SsoError as e:
        db.rollback()
        return _back(request, sso_error=str(e))
    settings = request.app.state.settings
    token = create_access_token(user_id, org_id, settings.jwt_secret, settings.token_ttl_minutes)
    return _back(request, sso_token=token)


def _user_for(db: Session, org_id: str, email: str, config: SsoConfigRow) -> str:
    """The account for ``email`` in this company, made on first sign-in with the default role."""
    db.commit()  # end the org transaction: the email lookup must search every company
    db.info["auth_lookup"] = True
    found = db.execute(select(UserRow.id, UserRow.org_id, UserRow.is_active).where(UserRow.email == email)).first()
    db.rollback()
    db.info["auth_lookup"] = False
    db.info["org_id"] = org_id
    if found is not None:
        user_id, user_org, active = found
        if user_org != org_id:
            raise SsoError(f"{email} already has an account in another company.")
        if not active:
            raise SsoError("This account is turned off. Ask your admin.")
        return user_id
    user = UserRow(org_id=org_id, email=email, password_hash=NO_PASSWORD, roles=[config.default_role])
    db.add(user)
    db.flush()
    team = db.scalar(select(TeamRow).where(TeamRow.org_id == org_id).order_by(TeamRow.name != "default",
                                                                              TeamRow.name))
    if team is not None:
        db.add(TeamMemberRow(org_id=org_id, team_id=team.id, user_id=user.id))
    record(db, org_id, user.id, "user.created", "user", user.id,
           {"email": email, "roles": [config.default_role], "via": "sso"})
    return user.id


def password_login_blocked(db: Session, org_id: str, roles: list[str]) -> bool:
    """When a company requires its own sign-in, only admins may still use a password (so a broken
    provider cannot lock everyone out). Call inside the login lookup."""
    if Role.ADMIN.value in roles:
        return False
    return bool(db.scalar(select(SsoConfigRow.enforce).where(SsoConfigRow.org_id == org_id)))
