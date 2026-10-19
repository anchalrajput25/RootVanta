"""FastAPI application.

Run locally:  uvicorn incident_agent.api.app:app --reload
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI
from sqlalchemy import text

from incident_agent import __version__
from incident_agent.api import (routes_auth, routes_incidents, routes_integrations, routes_knowledge, routes_org,
                                routes_sso, routes_workflow)
from incident_agent.api.config import Settings
from incident_agent.db.session import init_db, make_engine, make_session_factory
from incident_agent.integrations.crypto import SecretBox
from incident_agent.integrations.store import OrgConnections
from incident_agent.workflow.config import Connections

API_PREFIX = "/api/v1"


def create_app(settings: Optional[Settings] = None, connections: Optional[Connections] = None,
               secret_box: Optional[SecretBox] = None) -> FastAPI:
    """``connections`` are shared by every company that has not saved its own. Leave it out
    for a service used by several companies; set INCIDENT_AGENT_SHARED_CONNECTIONS=1 for a
    single-company install configured with environment variables."""
    settings = settings or Settings.from_env()
    if connections is None and os.environ.get("INCIDENT_AGENT_SHARED_CONNECTIONS") == "1":
        connections = Connections.from_env()
    engine = make_engine(settings.database_url)
    init_db(engine)

    app = FastAPI(title="Rootvanta", version=__version__)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.secret_box = secret_box or SecretBox.from_env()
    app.state.connections = OrgConnections(app.state.session_factory, app.state.secret_box, fallback=connections)

    for module in (routes_auth, routes_sso, routes_org, routes_incidents, routes_workflow, routes_integrations,
                   routes_knowledge):
        app.include_router(module.router, prefix=API_PREFIX)

    @app.get("/healthz", tags=["health"])
    def healthz() -> dict[str, str]:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app


def __getattr__(name: str):  # lazily build the app for `uvicorn incident_agent.api.app:app`
    if name == "app":
        return create_app()
    raise AttributeError(name)
