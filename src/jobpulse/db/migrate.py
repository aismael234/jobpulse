"""Programmatic Alembic access for ``jobpulse db …`` and ``jobpulse up``.

The migration scripts ship inside the package (``jobpulse/migrations``), so an installed
jobpulse migrates itself without alembic.ini or a checkout. alembic.ini at the project root
is only for ``uv run alembic revision --autogenerate``.
"""

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

SCRIPT_LOCATION = "jobpulse:migrations"


def alembic_config(database_url: str | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    # Passed as an attribute rather than `sqlalchemy.url`: configparser would choke on a
    # `%` in a URL-encoded password.
    if database_url is not None:
        config.attributes["database_url"] = database_url
    return config


def upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def show_current(database_url: str) -> None:
    command.current(alembic_config(database_url))


def head_revision() -> str | None:
    """The newest migration this code ships."""
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def schema_revisions(engine: Engine) -> tuple[str | None, str | None]:
    """The database's migration revision and the newest one this code ships."""
    return current_revision(engine), head_revision()
