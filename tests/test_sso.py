"""Company sign-in with OpenID Connect, against a fake provider that signs real ID tokens."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from conftest import PASSWORD, auth
from incident_agent.auth.oidc import OidcClient

ISSUER = "https://idp.test"
CLIENT_ID = "incident-agent"
SECRET = "client-secret"


class FakeProvider:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwks = {"keys": [{**jwk, "kid": "k1", "alg": "RS256", "use": "sig"}]}
        self.codes: dict[str, dict] = {}  # code -> what the authorize request carried
        self.claims: dict = {}  # extra or overriding claims for the next ID token
        self.token_calls: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/jwks"})
        if url == f"{ISSUER}/jwks":
            return httpx.Response(200, json=self.jwks)
        if url == f"{ISSUER}/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_calls.append(form)
            sent = self.codes.get(form.get("code", ""))
            if sent is None or form["client_secret"] != SECRET:
                return httpx.Response(400, json={"error": "invalid_grant"})
            digest = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
            if digest.decode() != sent["code_challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE failed"})
            now = int(time.time())
            claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "u-1", "iat": now, "exp": now + 300,
                      "nonce": sent["nonce"], "email": sent.get("login_hint", ""), "email_verified": True,
                      **self.claims}
            token = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})
            return httpx.Response(200, json={"id_token": token, "access_token": "x", "token_type": "Bearer"})
        return httpx.Response(404)

    def approve(self, authorize_url: str) -> str:
        """The person signs in at the provider; it sends back a code."""
        q = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
        assert q["client_id"] == CLIENT_ID and q["code_challenge_method"] == "S256"
        code = f"code-{len(self.codes)}"
        self.codes[code] = q
        return code


@pytest.fixture
def idp(client):
    provider = FakeProvider()
    client.app.state.oidc = OidcClient(http=httpx.Client(transport=httpx.MockTransport(provider.handler)))
    return provider


def setup_sso(client, admin_token, **overrides):
    body = {"domain": "acme.com", "issuer": ISSUER, "client_id": CLIENT_ID, "client_secret": SECRET,
            "default_role": "responder", **overrides}
    r = client.put("/sso", headers=auth(admin_token), json=body)
    assert r.status_code == 200, r.text
    return r.json()


def sign_in(client, idp, email, state_override=None):
    """Returns the fragment the browser lands on, e.g. {'sso_token': ...} or {'sso_error': ...}."""
    r = client.get("/auth/sso/start", params={"email": email}, follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    if "#" in location:  # sent straight back with an error
        return {k: v[0] for k, v in parse_qs(location.split("#", 1)[1]).items()}
    code = idp.approve(location)
    state = state_override or parse_qs(urlparse(location).query)["state"][0]
    r = client.get("/auth/sso/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("http://localhost:3001/#")
    return {k: v[0] for k, v in parse_qs(r.headers["location"].split("#", 1)[1]).items()}


def test_first_sign_in_creates_the_account_with_the_default_role(client, company, idp):
    org = company("Acme")
    out = setup_sso(client, org["admin"])
    assert out["redirect_uri"] == "http://localhost:3001/api/v1/auth/sso/callback"
    assert out["has_secret"] and "client_secret" not in out

    landed = sign_in(client, idp, "New.Person@acme.com")
    me = client.get("/auth/me", headers=auth(landed["sso_token"])).json()
    assert me["email"] == "new.person@acme.com" and me["roles"] == ["responder"]
    assert len(me["team_ids"]) == 1

    again = sign_in(client, idp, "new.person@acme.com")  # same account the second time
    assert client.get("/auth/me", headers=auth(again["sso_token"])).json()["id"] == me["id"]
    # The secret went to the provider, PKCE was used, and the account has no usable password.
    assert idp.token_calls[0]["client_secret"] == SECRET and idp.token_calls[0]["code_verifier"]
    assert client.post("/auth/login", json={"email": "new.person@acme.com", "password": "sso-only"}).status_code == 401


def test_existing_people_keep_their_account_and_roles(client, company, idp):
    org = company("Acme")
    setup_sso(client, org["admin"])
    landed = sign_in(client, idp, "approver@acme.com")
    assert client.get("/auth/me", headers=auth(landed["sso_token"])).json()["roles"] == ["approver"]


def test_enforced_sign_in_blocks_passwords_except_for_admins(client, company, idp):
    org = company("Acme")
    setup_sso(client, org["admin"], enforce=True)
    r = client.post("/auth/login", json={"email": "responder@acme.com", "password": PASSWORD})
    assert r.status_code == 403 and "company account" in r.json()["detail"]
    # Wrong password still says only "wrong", so the rule reveals nothing to a guesser.
    assert client.post("/auth/login", json={"email": "responder@acme.com", "password": "nope"}).status_code == 401
    assert client.post("/auth/login", json={"email": "admin@acme.com", "password": PASSWORD}).status_code == 200


@pytest.mark.parametrize("claims,error", [
    ({"aud": "someone-else"}, "not valid"),
    ({"iss": "https://evil.test"}, "not valid"),
    ({"exp": int(time.time()) - 3600}, "not valid"),
    ({"nonce": "replayed"}, "nonce"),
    ({"email_verified": False}, "not verified"),
    ({"email": "boss@other.com"}, "is not a acme.com address"),
    ({"email": None}, "did not share an email"),
])
def test_tokens_that_do_not_check_out_are_refused(client, company, idp, claims, error):
    org = company("Acme")
    setup_sso(client, org["admin"])
    idp.claims = claims
    landed = sign_in(client, idp, "someone@acme.com")
    assert "sso_token" not in landed and error in landed["sso_error"]


def test_a_token_signed_by_another_key_is_refused(client, company, idp):
    org = company("Acme")
    setup_sso(client, org["admin"])
    idp.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # jwks still has the old key
    assert "not valid" in sign_in(client, idp, "someone@acme.com")["sso_error"]


def test_a_mismatched_state_is_refused(client, company, idp):
    org = company("Acme")
    setup_sso(client, org["admin"])
    assert "does not match" in sign_in(client, idp, "someone@acme.com", state_override="forged")["sso_error"]


def test_callback_without_the_sign_in_cookie_is_refused(client, company, idp):
    org = company("Acme")
    setup_sso(client, org["admin"])
    r = client.get("/auth/sso/callback", params={"code": "x", "state": "y"}, follow_redirects=False)
    assert "try%20again" in r.headers["location"]


def test_unknown_domain_is_sent_back_with_a_message(client, idp):
    landed = sign_in(client, idp, "someone@nowhere.com")
    assert "not set up for nowhere.com" in landed["sso_error"]


def test_one_domain_belongs_to_one_company(client, company, idp):
    acme, beta = company("Acme"), company("Beta")
    setup_sso(client, acme["admin"])
    r = client.put("/sso", headers=auth(beta["admin"]), json={
        "domain": "acme.com", "issuer": ISSUER, "client_id": "x", "client_secret": "y"})
    assert r.status_code == 409


def test_an_email_already_in_another_company_cannot_join_by_sso(client, company, idp):
    acme, beta = company("Acme"), company("Beta")
    team = client.get("/teams", headers=auth(beta["admin"])).json()[0]["id"]
    r = client.post("/users", headers=auth(beta["admin"]), json={
        "email": "contractor@acme.com", "password": PASSWORD, "roles": ["viewer"], "team_ids": [team]})
    assert r.status_code == 201
    setup_sso(client, acme["admin"])
    assert "another company" in sign_in(client, idp, "contractor@acme.com")["sso_error"]


def test_only_admins_see_or_change_sign_in_settings(client, company, idp):
    org = company("Acme")
    assert client.get("/sso", headers=auth(org["responder"])).status_code == 403
    assert client.put("/sso", headers=auth(org["approver"]), json={
        "domain": "acme.com", "issuer": ISSUER, "client_id": "x", "client_secret": "y"}).status_code == 403
    assert client.get("/sso", headers=auth(org["admin"])).json()["configured"] is False
    setup_sso(client, org["admin"])
    assert client.post("/sso/test", headers=auth(org["admin"])).json()["ok"] is True
    assert client.delete("/sso", headers=auth(org["admin"])).status_code == 204
    assert "not set up" in sign_in(client, idp, "x@acme.com")["sso_error"]


def test_settings_need_a_secret_and_a_safe_issuer(client, company):
    org = company("Acme")
    h = auth(org["admin"])
    base = {"domain": "acme.com", "issuer": ISSUER, "client_id": "x"}
    assert client.put("/sso", headers=h, json=base).status_code == 422
    r = client.put("/sso", headers=h, json={**base, "issuer": "http://idp.test", "client_secret": "s"})
    assert r.status_code == 422 and "https" in r.json()["detail"]
    r = client.put("/sso", headers=h, json={**base, "domain": "not a domain", "client_secret": "s"})
    assert r.status_code == 422
