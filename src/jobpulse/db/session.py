"""Engine and session setup. SQLite runs in WAL mode with foreign keys on."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

SQLITE_BUSY_TIMEOUT_MS = 30_000
DEFAULT_DATA_DIR = Path("data")
DATABASE_FILE = "jobpulse.db"


def database_url(data_dir: Path) -> str:
    """The SQLite database in ``data_dir`` (``--data-dir``, default ./data)."""
    return f"sqlite:///{data_dir / DATABASE_FILE}"


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
