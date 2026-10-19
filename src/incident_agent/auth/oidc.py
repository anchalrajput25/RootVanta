"""Company sign-in (single sign-on) with OpenID Connect.

Works with any OpenID Connect provider: Google Workspace, Microsoft Entra ID,
Okta, Auth0, Keycloak and others. The flow is the standard authorization-code
flow with PKCE:

1. ``start`` sends the browser to the company's provider, remembering a
   random state, nonce and PKCE verifier in a short-lived signed cookie.
2. The provider sends the browser back with a code. ``finish`` swaps the code
   for an ID token, checks its signature against the provider's published
   keys, its issuer, audience, expiry and nonce, and returns the email.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
import jwt

TIMEOUT = 10.0
CACHE_SECONDS = 3600
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]
CLOCK_SKEW_SECONDS = 60


class SsoError(Exception):
    """Sign-in failed; the message is safe to show to the person signing in."""


@dataclass(frozen=True)
class Provider:
    issuer: str
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class Pending:
    """What must come back unchanged from the provider: kept in a signed cookie, never in the URL."""

    state: str
    nonce: str
    verifier: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def new_pending() -> Pending:
    return Pending(state=secrets.token_urlsafe(24), nonce=secrets.token_urlsafe(24), verifier=secrets.token_urlsafe(48))


def challenge(verifier: str) -> str:
    return _b64(hashlib.sha256(verifier.encode()).digest())


@dataclass
class OidcClient:
    http: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=TIMEOUT))
    _cache: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)

    def _get_json(self, url: str, what: str) -> dict[str, Any]:
        cached = self._cache.get(url)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        try:
            r = self.http.get(url)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise SsoError(f"Could not read the sign-in provider's {what}: {e}") from e
        self._cache[url] = (time.monotonic(), data)
        return data

    def discover(self, issuer: str) -> dict[str, Any]:
        doc = self._get_json(issuer.rstrip("/") + "/.well-known/openid-configuration", "settings")
        if doc.get("issuer", "").rstrip("/") != issuer.rstrip("/"):
            raise SsoError(f"The provider calls itself {doc.get('issuer')!r}, not {issuer!r}. Check the issuer address.")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not doc.get(key):
                raise SsoError(f"The provider's settings have no {key}")
        return doc

    def authorize_url(self, p: Provider, redirect_uri: str, pending: Pending, login_hint: str = "") -> str:
        doc = self.discover(p.issuer)
        params = {
            "response_type": "code", "client_id": p.client_id, "redirect_uri": redirect_uri,
            "scope": "openid email profile", "state": pending.state, "nonce": pending.nonce,
            "code_challenge": challenge(pending.verifier), "code_challenge_method": "S256",
        }
        if login_hint:
            params["login_hint"] = login_hint
        sep = "&" if "?" in doc["authorization_endpoint"] else "?"
        return doc["authorization_endpoint"] + sep + urlencode(params)

    def finish(self, p: Provider, redirect_uri: str, code: str, pending: Pending) -> dict[str, Any]:
        """Swap the code for an ID token and return its checked claims."""
        doc = self.discover(p.issuer)
        try:
            r = self.http.post(doc["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "client_id": p.client_id, "client_secret": p.client_secret, "code_verifier": pending.verifier,
            }, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise SsoError(f"Could not reach the sign-in provider: {e}") from e
        if r.status_code >= 400:
            try:
                reason = r.json().get("error_description") or r.json().get("error")
            except ValueError:
                reason = r.text[:200]
            raise SsoError(f"The sign-in provider refused the login: {reason}")
        id_token = (r.json() or {}).get("id_token")
        if not id_token:
            raise SsoError("The sign-in provider sent no ID token")
        return self.verify(p, doc, id_token, pending.nonce)

    def verify(self, p: Provider, doc: dict[str, Any], id_token: str, nonce: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as e:
            raise SsoError("The ID token is not readable") from e
        alg = header.get("alg")
        if alg not in ALGORITHMS:
            raise SsoError(f"The ID token uses an algorithm that is not accepted ({alg})")
        keys = self._get_json(doc["jwks_uri"], "signing keys").get("keys") or []
        match = [k for k in keys if k.get("kid") == header.get("kid")] if header.get("kid") else keys
        if not match:  # the provider may have rotated its keys: read them again once
            self._cache.pop(doc["jwks_uri"], None)
            keys = self._get_json(doc["jwks_uri"], "signing keys").get("keys") or []
            match = [k for k in keys if k.get("kid") == header.get("kid")]
        if not match:
            raise SsoError("The ID token was signed with an unknown key")
        try:
            key = jwt.PyJWK(match[0], algorithm=alg)
            claims = jwt.decode(id_token, key, algorithms=[alg], audience=p.client_id, issuer=doc["issuer"],
                                leeway=CLOCK_SKEW_SECONDS, options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.PyJWTError as e:
            raise SsoError(f"The ID token is not valid: {e}") from e
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise SsoError("The ID token does not belong to this sign-in (nonce mismatch)")
        if claims.get("email_verified") is False:
            raise SsoError("The provider says this email address is not verified")
        return claims


def email_of(claims: dict[str, Any]) -> Optional[str]:
    """The email the provider vouches for. Microsoft Entra may send it as preferred_username."""
    email = claims.get("email") or claims.get("preferred_username")
    if isinstance(email, str) and "@" in email:
        return email.strip().lower()
    return None
