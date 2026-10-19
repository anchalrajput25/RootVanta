"""Read log lines from Loki."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx


class LokiError(RuntimeError):
    pass


@dataclass(frozen=True)
class LogLine:
    at: datetime
    labels: dict[str, str]
    line: str

    def json(self) -> dict[str, Any]:
        """The line parsed as JSON, or {"msg": line} for plain text."""
        try:
            value = json.loads(self.line)
        except ValueError:
            return {"msg": self.line}
        return value if isinstance(value, dict) else {"msg": self.line}


class LokiClient:
    def __init__(self, base_url: str, http: Optional[httpx.Client] = None):
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=10.0)

    def query_range(self, query: str, start: datetime, end: datetime, limit: int = 5000) -> list[LogLine]:
        params = {
            "query": query,
            "start": str(int(start.timestamp() * 1e9)),
            "end": str(int(end.timestamp() * 1e9)),
            "limit": limit,
            "direction": "forward",
        }
        try:
            r = self.http.get(f"{self.base_url}/loki/api/v1/query_range", params=params)
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise LokiError(f"Loki query failed: {e}") from e
        if body.get("status") != "success":
            raise LokiError(f"Loki error: {body}")
        lines = [
            LogLine(datetime.fromtimestamp(int(ts) / 1e9, tz=timezone.utc), stream.get("stream", {}), line)
            for stream in body["data"]["result"]
            for ts, line in stream["values"]
        ]
        return sorted(lines, key=lambda x: x.at)
