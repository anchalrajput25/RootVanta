from __future__ import annotations

import os
from typing import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from incident_agent.api.app import API_PREFIX, create_app
from incident_agent.api.config import Settings

# Encrypts saved monitoring tokens in tests; without it a key file would be written to disk.
os.environ.setdefault("INCIDENT_AGENT_SECRET_KEY", "test-key-for-saved-tokens")

PASSWORD = "correct-horse-battery"
JWT_SECRET = "test-secret-that-is-long-enough-for-hs256"


def postgres_url() -> str | None:
    """Set TEST_DATABASE_URL to run the API tests on PostgreSQL as well, e.g.
    postgresql+psycopg://incident_app:incident_app@localhost:5432/incident_test
    The role must not be a superuser, or row-level security is bypassed."""
    return os.environ.get("TEST_DATABASE_URL")


def reset_postgres(url: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def database_url(request) -> str:
    if request.param == "sqlite":
        return "sqlite://"
    url = postgres_url()
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    reset_postgres(url)
    return url


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    app = create_app(Settings(database_url=database_url, jwt_secret=JWT_SECRET))
    with TestClient(app, base_url=f"http://test{API_PREFIX}") as c:
        yield c
    app.state.engine.dispose()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def company(client: TestClient) -> Callable[[str], dict]:
    """Create an organization with an admin, a responder, an approver and a viewer."""

    def make(name: str) -> dict:
        slug = name.lower()
        r = client.post("/auth/signup", json={
            "organization_name": name, "email": f"admin@{slug}.com", "password": PASSWORD,
        })
        assert r.status_code == 201, r.text
        admin = r.json()["access_token"]
        team_id = client.get("/teams", headers=auth(admin)).json()[0]["id"]

        tokens = {"admin": admin}
        for role in ("responder", "approver", "viewer"):
            email = f"{role}@{slug}.com"
            r = client.post("/users", headers=auth(admin), json={
                "email": email, "password": PASSWORD, "roles": [role], "team_ids": [team_id],
            })
            assert r.status_code == 201, r.text
            tokens[role] = client.post("/auth/login", json={"email": email, "password": PASSWORD}).json()[
                "access_token"
            ]
        return {"team_id": team_id, **tokens}

    return make
