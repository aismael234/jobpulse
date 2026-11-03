"""Alembic environment. jobpulse passes the database URL in code (``db.migrate``); the
``alembic`` command (autogenerate) uses ./data/jobpulse.db, as the app does by default."""

import logging
from logging.config import fileConfig
from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext

from jobpulse.db.session import DEFAULT_DATA_DIR, create_engine_for
from jobpulse.db.session import database_url as default_database_url
from jobpulse.db.tables import Base
from jobpulse.db.types import UTCDateTime

config = context.config
# Leave logging alone when the app (or pytest) has already configured it.
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def database_url() -> str:
    explicit = config.attributes.get("database_url") or config.get_main_option("sqlalchemy.url")
    if explicit:
        return str(explicit)
    return default_database_url(DEFAULT_DATA_DIR)


def render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
    # Migrations store the plain impl type, so they never import application code.
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def configure(**kwargs: Any) -> None:
    url = database_url()
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=url.startswith("sqlite"),
        compare_type=True,
        render_item=render_item,
        **kwargs,
    )


def run_migrations_offline() -> None:
    configure(url=database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine_for(database_url())
    try:
        with engine.connect() as connection:
            configure(connection=connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
