"""Send short messages to each organization's channels (Slack-style incoming webhooks).

Each message links to the incident in the dashboard when the dashboard's
address is known (INCIDENT_AGENT_PUBLIC_URL), so people can approve a fix or
read the evidence in one click.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from incident_agent.api.audit import record
from incident_agent.db.models import NotificationTargetRow

log = logging.getLogger(__name__)


@dataclass
class Notifier:
    http: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=5.0))
    base_url: str = field(default_factory=lambda: os.environ.get("INCIDENT_AGENT_PUBLIC_URL", "").rstrip("/"))

    def link(self, incident_id: Optional[str]) -> str:
        if not self.base_url or not incident_id:
            return ""
        return f"{self.base_url}/?incident={quote(incident_id)}"

    def send(self, db: Session, org_id: str, text: str, incident_id: Optional[str] = None) -> int:
        """Post ``text`` to every target of the organization. Returns how many accepted it.

        A failed delivery never stops the incident work; it is written to the audit log."""
        url = self.link(incident_id)
        # Slack shows <url|label> as a link; other chat tools still show a usable address.
        payload = {"text": f"{text}\n<{url}|Open in Rootvanta>" if url else text, "unfurl_links": False}
        sent = 0
        targets = db.scalars(select(NotificationTargetRow).where(NotificationTargetRow.org_id == org_id)).all()
        for t in targets:
            try:
                self.http.post(t.url, json=payload).raise_for_status()
                sent += 1
            except httpx.HTTPError as e:
                log.warning("notification to %s failed: %s", t.id, e)
                record(db, org_id, None, "notification.failed", "notification_target", t.id, {"error": str(e)[:300]})
        return sent
