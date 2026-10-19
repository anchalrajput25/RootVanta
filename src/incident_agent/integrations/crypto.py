"""Encrypts the tokens companies save for their monitoring, so a database copy alone does not leak them.

The key comes from INCIDENT_AGENT_SECRET_KEY. Without it, it is derived from
INCIDENT_AGENT_JWT_SECRET; without either (a local demo), a key file is created
next to the app so the API and the workers share it.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

KEY_FILE = Path(".incident_agent.key")


class SecretBox:
    def __init__(self, key_material: str):
        key = base64.urlsafe_b64encode(hashlib.sha256(key_material.encode()).digest())
        self._fernet = Fernet(key)

    def seal(self, text: str) -> str:
        return self._fernet.encrypt(text.encode()).decode()

    def open(self, sealed: str) -> str:
        try:
            return self._fernet.decrypt(sealed.encode()).decode()
        except InvalidToken:
            raise ValueError("a saved token cannot be read: the secret key changed; save the token again") from None

    @classmethod
    def from_env(cls, key_file: Optional[Path] = None) -> "SecretBox":
        material = os.environ.get("INCIDENT_AGENT_SECRET_KEY") or os.environ.get("INCIDENT_AGENT_JWT_SECRET")
        if material:
            return cls(material)
        path = key_file or KEY_FILE
        if not path.exists():
            log.warning("INCIDENT_AGENT_SECRET_KEY is not set; creating %s for this computer", path.resolve())
            path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        return cls(path.read_text(encoding="utf-8").strip())
