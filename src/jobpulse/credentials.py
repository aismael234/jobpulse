"""Secrets from ``.env`` (and the environment) or from Settings (M6).

A value in ``.env`` always wins, so an install configured by hand keeps working and a value
can be pinned there. Settings stores the rest in the database (``db.secrets``). Every value
read or saved is added to the process's ``SecretRegistry``, so logs and pages mask it from
then on, without a restart.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.orm import Session

from jobpulse.config import Settings, secret_problem
from jobpulse.db import secrets as secret_rows
from jobpulse.logging_setup import SecretRegistry

Source = Literal["env", "settings", "unset"]

SECRET_NAMES = ("ntfy_topic", "ntfy_token", "discord_webhook_url", "llm_api_key")
SECRET_LABELS = {
    "ntfy_topic": "ntfy topic",
    "ntfy_token": "ntfy access token",
    "discord_webhook_url": "Discord webhook URL",
    "llm_api_key": "Anthropic API key",
}
MAX_SECRET_CHARS = 500


class SecretError(ValueError):
    """A secret that can't be saved; the message never contains the value."""


@dataclass(frozen=True, slots=True)
class Secrets:
    values: Mapping[str, str]  # only the secrets that are set
    sources: Mapping[str, Source]

    def get(self, name: str) -> str | None:
        return self.values.get(name)


def env_secrets(settings: Settings | None) -> dict[str, str]:
    """The secrets set in ``.env`` or the environment, by name."""
    values: dict[str, str] = {}
    for name in SECRET_NAMES:
        secret = getattr(settings, name, None) if settings is not None else None
        if secret is not None and (value := secret.get_secret_value()):
            values[name] = value
    return values


class SecretStore:
    """Resolves each secret (``.env`` first, then Settings) and saves Settings' values."""

    def __init__(
        self, settings: Settings | None = None, registry: SecretRegistry | None = None
    ) -> None:
        self.settings = settings
        self.registry = registry if registry is not None else SecretRegistry()
        self.registry.add(self._env().values())

    def _env(self) -> dict[str, str]:
        return env_secrets(self.settings)

    def resolve(self, session: Session) -> Secrets:
        env = self._env()
        saved = {name: value for name, value in secret_rows.get_all(session).items() if value}
        self.registry.add(saved.values())
        values: dict[str, str] = {}
        sources: dict[str, Source] = {}
        for name in SECRET_NAMES:
            if name in env:
                values[name], sources[name] = env[name], "env"
            elif name in saved:
                values[name], sources[name] = saved[name], "settings"
            else:
                sources[name] = "unset"
        return Secrets(values, sources)

    def save(self, session: Session, name: str, value: str, *, now: datetime) -> None:
        """Store a Settings value; ``SecretError`` when it's unknown, pinned in ``.env`` or
        malformed."""
        label = self._label(name)
        if name in self._env():
            raise SecretError(f"the {label} is set in .env, which wins; change it there")
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
