"""Secrets saved on Settings, in the database (``db.secrets``).

Every value read or saved is added to the process's ``SecretRegistry``, so logs and pages
mask it from then on, without a restart. The dashboard password is different: only a salted
scrypt hash of it is stored, since it only ever needs checking.
"""

import base64
import hashlib
import hmac
import secrets as token_source
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.orm import Session

from jobpulse.config import secret_problem
from jobpulse.db import secrets as secret_rows
from jobpulse.logging_setup import SecretRegistry

Source = Literal["settings", "unset"]

SECRET_NAMES = ("ntfy_topic", "ntfy_token", "discord_webhook_url", "llm_api_key")
SECRET_LABELS = {
    "ntfy_topic": "ntfy topic",
    "ntfy_token": "ntfy access token",
    "discord_webhook_url": "Discord webhook URL",
    "llm_api_key": "Anthropic API key",
}
MAX_SECRET_CHARS = 500
PASSWORD_ROW = "dashboard_password"  # its scrypt hash, never the password
MIN_PASSWORD_CHARS = 8
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


class SecretError(ValueError):
    """A secret that can't be saved; the message never contains the value."""


@dataclass(frozen=True, slots=True)
class Secrets:
    values: Mapping[str, str]  # only the secrets that are set
    sources: Mapping[str, Source]

    def get(self, name: str) -> str | None:
        return self.values.get(name)


class SecretStore:
    """Reads and saves the secrets on Settings, adding each value to ``registry``."""

    def __init__(self, registry: SecretRegistry | None = None) -> None:
        self.registry = registry if registry is not None else SecretRegistry()

    def resolve(self, session: Session) -> Secrets:
        saved = {
            name: value
            for name, value in secret_rows.get_all(session).items()
            if value and name in SECRET_NAMES
        }
        self.registry.add(saved.values())
        sources: dict[str, Source] = {
            name: "settings" if name in saved else "unset" for name in SECRET_NAMES
        }
        return Secrets(saved, sources)

    def save(self, session: Session, name: str, value: str, *, now: datetime) -> None:
        """Store a value; ``SecretError`` when the name is unknown or the value malformed."""
        label = self._label(name)
        value = value.strip()
        if not value:
            raise SecretError(f"enter the {label}, or clear it")
        if len(value) > MAX_SECRET_CHARS or any(ord(char) < 32 for char in value):
            raise SecretError(f"that isn't a valid {label}")
        if problem := secret_problem(name, value):
            raise SecretError(f"the {label} {problem}")
        self.registry.add([value])
        secret_rows.put(session, name, value, now=now)

    def clear(self, session: Session, name: str) -> bool:
        self._label(name)
        return secret_rows.clear(session, name)

    @staticmethod
    def _label(name: str) -> str:
        if name not in SECRET_NAMES:
            raise SecretError(f"unknown secret {name!r}")
        return SECRET_LABELS[name]


# --- the dashboard password ----------------------------------------------------------------


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else token_source.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + "$".join(base64.b64encode(part).decode() for part in (salt, digest))


def password_matches(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        given = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), **_SCRYPT)
    except ValueError:
        return False
    return hmac.compare_digest(given, expected)


def password_hash(session: Session) -> str | None:
    """The stored hash; ``None`` while the dashboard has no password."""
    return secret_rows.get_all(session).get(PASSWORD_ROW) or None


def set_password(session: Session, password: str, *, now: datetime) -> None:
    if len(password) < MIN_PASSWORD_CHARS:
        raise SecretError(f"use at least {MIN_PASSWORD_CHARS} characters")
    if len(password) > MAX_SECRET_CHARS:
        raise SecretError("that password is too long")
    secret_rows.put(session, PASSWORD_ROW, hash_password(password), now=now)


def clear_password(session: Session) -> bool:
    return secret_rows.clear(session, PASSWORD_ROW)
