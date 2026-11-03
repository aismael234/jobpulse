"""``jobpulse up`` (M8): the dashboard and the scheduler in one process, for a new install.

On start it backs up an SQLite database that needs an upgrade (into ``backups/`` next to the
file, keeping three), migrates, takes the worker lease, and serves the dashboard with the
worker in its own thread. The poller's database phases are synchronous and big boards take
seconds to parse, so the worker runs its own event loop rather than sharing uvicorn's. A
watchdog stall, or a lost lease, makes the process exit with code 1 so a supervisor restarts
it. Everything else comes from the database: the profile, its Sources & schedule
settings and the secrets, all edited on the dashboard.
"""

import asyncio
import ipaddress
import logging
import os
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import uvicorn
from sqlalchemy import inspect
from sqlalchemy.engine import make_url

from jobpulse.config import InfraConfig
from jobpulse.core.timeparse import utcnow
from jobpulse.credentials import SecretStore, password_hash
from jobpulse.db import lease, migrate
from jobpulse.db.session import create_engine_for, session_scope
from jobpulse.runtime import open_database, run_worker

log = logging.getLogger(__name__)

KEEP_BACKUPS = 3
STOP_TIMEOUT_SECONDS = 30.0


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def sqlite_path(database_url: str) -> Path | None:
    """The file behind an SQLite URL; ``None`` for other databases and in-memory ones."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    return Path(url.database)


def pending_upgrade(database_url: str) -> tuple[str | None, str | None]:
    """(the database's revision, the newest this code ships). A database without tables
    reads as ``None``."""
    engine = create_engine_for(database_url)
    try:
        if not inspect(engine).has_table("alembic_version"):
            return None, migrate.head_revision()
        return migrate.schema_revisions(engine)
    finally:
        engine.dispose()


def backup_sqlite(path: Path, folder: Path, *, revision: str, now: datetime) -> Path:
    """Copy the database with SQLite's online backup (safe while it's in use) into
    ``folder``, readable by its owner only, and keep the newest ``KEEP_BACKUPS``."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"jobpulse-{revision}-{now.strftime('%Y%m%dT%H%M%SZ')}.db"
    source = sqlite3.connect(path)
    try:
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()
    os.chmod(target, 0o600)
    backups = sorted(folder.glob("jobpulse-*.db"), key=lambda found: found.stat().st_mtime)
    for old in backups[:-KEEP_BACKUPS]:
        old.unlink()
    return target


def prepare_database(database_url: str, *, now: datetime) -> Path | None:
    """Back up an SQLite database that is behind, then migrate to the newest revision;
    the backup's path, if one was taken."""
    current, head = pending_upgrade(database_url)
    backup = None
    path = sqlite_path(database_url)
    if current is not None and current != head and path is not None and path.exists():
        backup = backup_sqlite(path, path.parent / "backups", revision=current, now=now)
        log.info("backed up the database (at %s) to %s before upgrading", current, backup)
    elif path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    if current != head:
        migrate.upgrade(database_url)
        log.info("database upgraded to %s", head)
    return backup


class WorkerThread:
    """``run_worker`` on its own thread and event loop, stopped from another thread.
    ``on_exit`` gets the exception it ended with (``None`` after ``stop``)."""

    def __init__(
        self,
        *,
        store: SecretStore,
        database_url: str,
        holder: lease.Holder,
        data_dir: Path | None = None,
        base: InfraConfig | None = None,
        on_exit: Callable[[BaseException | None], None] = lambda error: None,
    ) -> None:
        self._data_dir = data_dir
        self._base = base
        self._store = store
        self._database_url = database_url
        self._holder = holder
        self._on_exit = on_exit
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._stopping = False
        self._thread: threading.Thread | None = None
        self.error: BaseException | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="jobpulse-worker", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        stop = asyncio.Event()
        with self._lock:
            self._loop, self._stop = loop, stop
            if self._stopping:
                stop.set()
        error: BaseException | None = None
        try:
            loop.run_until_complete(
                run_worker(
                    store=self._store,
                    database_url=self._database_url,
                    data_dir=self._data_dir,
                    base=self._base,
                    stop=stop,
                    holder=self._holder,
                )
            )
        except BaseException as exc:
            error = exc
            log.error("worker: %s", exc)
        finally:
            loop.close()
        self.error = error
        self._on_exit(error)

    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        with self._lock:
            self._stopping = True
            if self._loop is not None and self._stop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout)


def run_app(
    *,
    store: SecretStore,
    database_url: str,
    host: str,
    port: int,
    data_dir: Path | None = None,
    migrate_first: bool = True,
) -> int:
    """Serve the dashboard with the worker in a thread until SIGINT or SIGTERM; the exit
    code (1 when the worker stalled or lost its lease). Raises ``LeaseHeld`` before serving
    when another worker polls this database."""
    from jobpulse.web.app import create_app  # the web layer imports runtime, not the reverse

    if migrate_first:
        prepare_database(database_url, now=utcnow())
    sessions = open_database(database_url)
    me = lease.Holder.here()
    with session_scope(sessions) as session:
        lease.acquire(session, me, now=utcnow())
        protected = password_hash(session) is not None
    log.info("jobpulse on http://%s:%d/ (password %s)", host, port, "on" if protected else "off")
    if not protected and not is_loopback(host):
        log.warning(
            "the dashboard listens on %s with no password: anyone who can reach port %d can "
            "use it. Set one on Settings → Security, or publish the port on 127.0.0.1 only "
            "(as the Docker instructions do)",
            host,
            port,
        )
    server: uvicorn.Server | None = None

    def worker_ended(error: BaseException | None) -> None:
        if error is not None and server is not None:
            server.should_exit = True

    worker = WorkerThread(
        store=store,
        database_url=database_url,
        holder=me,
        data_dir=data_dir,
        on_exit=worker_ended,
    )
    application = create_app(sessions=sessions, store=store, worker=worker)
    server = uvicorn.Server(
        uvicorn.Config(application, host=host, port=port, log_config=None, server_header=False)
    )
    try:
        server.run()
    finally:
        with session_scope(sessions) as session:
            lease.release(session, me)
    return 1 if worker.error is not None else 0
