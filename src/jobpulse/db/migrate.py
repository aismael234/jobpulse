"""Programmatic Alembic access for ``jobpulse db …`` and container start-up."""

import os
from pathlib import Path

from alembic import command
from alembic.config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def find_alembic_ini() -> Path:
    """``$JOBPULSE_ALEMBIC_INI``, then the project root, then the working directory."""
    candidates = [Path(path) for path in (os.environ.get("JOBPULSE_ALEMBIC_INI"),) if path]
    candidates += [PROJECT_ROOT / "alembic.ini", Path.cwd() / "alembic.ini"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "alembic.ini not found; run from the project root or set JOBPULSE_ALEMBIC_INI"
    )


def alembic_config(database_url: str) -> Config:
    config = Config(str(find_alembic_ini()))
    # Passed as an attribute rather than `sqlalchemy.url`: configparser would choke on a
    # `%` in a URL-encoded password.
    config.attributes["database_url"] = database_url
    return config


def upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def show_current(database_url: str) -> None:
    command.current(alembic_config(database_url))
