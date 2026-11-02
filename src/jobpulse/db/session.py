"""Engine and session setup. SQLite runs in WAL mode with foreign keys on."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from jobpulse.config import DEFAULT_DATABASE_URL, InfraConfig, Settings

SQLITE_BUSY_TIMEOUT_MS = 30_000


def resolve_database_url(settings: Settings, config: InfraConfig | None) -> str:
    """``DATABASE_URL`` beats ``database_url`` in config.yaml, which beats the default."""
    if settings.database_url:
        return settings.database_url
    if config is not None:
        return config.database_url
    return DEFAULT_DATABASE_URL


def display_url(url: str) -> str:
    """The URL with any password masked, safe to print."""
    return make_url(url).render_as_string(hide_password=True)


def create_engine_for(url: str, *, echo: bool = False) -> Engine:
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return create_engine(url, echo=echo, pool_pre_ping=True)
    database = parsed.database
    if database and database != ":memory:" and not database.startswith("file:"):
        Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, echo=echo)
    event.listen(engine, "connect", _set_sqlite_pragmas)
    return engine


def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit on success, roll back on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
