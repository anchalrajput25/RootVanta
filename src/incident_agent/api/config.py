"""Settings read from environment variables (see .env.example)."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    database_url: str
    jwt_secret: str
    token_ttl_minutes: int = 60
    # Where people open the dashboard, e.g. https://incidents.example.com. Used for
    # sign-in redirects and for links in chat messages.
    public_url: str = "http://localhost:3001"

    @classmethod
    def from_env(cls) -> "Settings":
        secret = os.environ.get("INCIDENT_AGENT_JWT_SECRET")
        if not secret:
            # Fine for a local demo; tokens stop working after a restart.
            log.warning("INCIDENT_AGENT_JWT_SECRET is not set; using a random secret for this process")
            secret = secrets.token_urlsafe(48)
        if len(secret) < 32:
            raise ValueError("INCIDENT_AGENT_JWT_SECRET must be at least 32 characters")
        return cls(
            database_url=os.environ.get("INCIDENT_AGENT_DATABASE_URL", "sqlite:///./incident_agent.db"),
            jwt_secret=secret,
            token_ttl_minutes=int(os.environ.get("INCIDENT_AGENT_TOKEN_TTL_MINUTES", "60")),
            public_url=os.environ.get("INCIDENT_AGENT_PUBLIC_URL", "http://localhost:3001").rstrip("/"),
        )
