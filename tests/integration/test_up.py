"""M8: `jobpulse up` backs up and upgrades the database, holds the worker lease, runs the
worker in a thread beside the dashboard, and exits with code 1 when the worker stalls; the
worker rebuilds its schedule when Settings → Sources & schedule changes (M10)."""

import asyncio
import os
import stat
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from typer.testing import CliRunner

from jobpulse.cli import app
from jobpulse.config import InfraConfig, Profile, SourceSettings
from jobpulse.core.timeparse import utcnow
from jobpulse.credentials import SecretStore, set_password
from jobpulse.db import lease, migrate
from jobpulse.db import profile as profile_db
from jobpulse.db.session import create_engine_for, make_session_factory, session_scope
from jobpulse.db.tables import WorkerLease
from jobpulse.poller import Poller
from jobpulse.runtime import (
    IDLE_RECHECK_SECONDS,
    WorkerStalled,
    open_database,
    run_scheduled_cycle,
    run_worker,
)
from jobpulse.up import KEEP_BACKUPS, WorkerThread, prepare_database, run_app
from tests.factories import NOW

ME = lease.Holder("host-a", 100, "aaaa")
OTHER = lease.Holder("host-b", 200, "bbbb")


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'data' / 'jobpulse.db'}"


@pytest.fixture
def migrated(db_url: str) -> str:
    prepare_database(db_url, now=NOW)
    return db_url


def lease_row(url: str) -> WorkerLease | None:
    with open_database(url)() as session:
        return session.get(WorkerLease, lease.LEASE_ID)


# --- the database ---------------------------------------------------------------------------


def test_a_new_database_is_created_at_head_without_a_backup(db_url: str, tmp_path: Path) -> None:
    assert prepare_database(db_url, now=NOW) is None
    engine = create_engine_for(db_url)
    assert migrate.schema_revisions(engine) == (migrate.head_revision(),) * 2
    assert not (tmp_path / "data" / "backups").exists()


def test_an_older_database_is_backed_up_then_upgraded(db_url: str, tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    migrate.upgrade(db_url, "0005")

    backup = prepare_database(db_url, now=NOW)

    assert backup is not None
    assert backup.parent == tmp_path / "data" / "backups"
    assert backup.name.startswith("jobpulse-0005-")
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600
    old = create_engine_for(f"sqlite:///{backup}")
    assert migrate.current_revision(old) == "0005"
    engine = create_engine_for(db_url)
    assert migrate.current_revision(engine) == migrate.head_revision()
    assert "worker_lease" in inspect(engine).get_table_names()


def test_backups_keep_the_newest_three(db_url: str, tmp_path: Path) -> None:
    folder = tmp_path / "data" / "backups"
    folder.mkdir(parents=True)
    for index in range(4):
        old = folder / f"jobpulse-0004-2026010{index}T000000Z.db"
        old.write_bytes(b"")
        os.utime(old, (1_000_000 + index, 1_000_000 + index))
    migrate.upgrade(db_url, "0009")
    prepare_database(db_url, now=NOW)
    left = sorted(path.name for path in folder.iterdir())
    assert len(left) == KEEP_BACKUPS
    assert any(name.startswith("jobpulse-0009-") for name in left)
    assert "jobpulse-0004-20260100T000000Z.db" not in left


def test_a_current_database_is_left_alone(migrated: str, tmp_path: Path) -> None:
    assert prepare_database(migrated, now=NOW) is None
    assert not (tmp_path / "data" / "backups").exists()


# --- the lease ------------------------------------------------------------------------------


def test_the_lease_admits_one_holder_until_it_expires(migrated: str) -> None:
    sessions = open_database(migrated)
    with session_scope(sessions) as session:
        lease.acquire(session, ME, now=NOW)
    with session_scope(sessions) as session, pytest.raises(lease.LeaseHeld, match="host-a:100"):
        lease.acquire(session, OTHER, now=NOW + timedelta(minutes=1))
    with session_scope(sessions) as session:
        assert lease.renew(session, ME, now=NOW + timedelta(minutes=4))
    with session_scope(sessions) as session, pytest.raises(lease.LeaseHeld):
        lease.acquire(session, OTHER, now=NOW + timedelta(minutes=8))
    with session_scope(sessions) as session:  # not renewed for 5 minutes: free
        lease.acquire(session, OTHER, now=NOW + timedelta(minutes=10))
    with session_scope(sessions) as session:
        assert not lease.renew(session, ME, now=NOW + timedelta(minutes=10))
        lease.release(session, ME)  # not ours: kept
    assert (row := lease_row(migrated)) is not None
    assert row.holder == str(OTHER)


def test_a_holder_on_this_host_whose_process_is_gone_is_free(migrated: str) -> None:
    sessions = open_database(migrated)
    with session_scope(sessions) as session:
        lease.acquire(session, ME, now=NOW)
    restarted = lease.Holder("host-a", 101, "cccc")
    with session_scope(sessions) as session, pytest.raises(lease.LeaseHeld):
        lease.acquire(session, restarted, now=NOW, alive=lambda pid: True)
    with session_scope(sessions) as session:
        lease.acquire(session, restarted, now=NOW, alive=lambda pid: pid != 100)
    same_pid = lease.Holder("host-a", 101, "dddd")  # a container restarted as the same pid
    with session_scope(sessions) as session:
        lease.acquire(session, same_pid, now=NOW, alive=lambda pid: True)


def test_a_second_worker_refuses_to_start(migrated: str) -> None:
    with session_scope(open_database(migrated)) as session:
        lease.acquire(session, OTHER, now=utcnow())
    with pytest.raises(lease.LeaseHeld):
        asyncio.run(run_worker(store=SecretStore(), database_url=migrated))


def test_a_worker_takes_the_lease_and_gives_it_back(migrated: str) -> None:
    async def run() -> None:
        stop = asyncio.Event()
        stop.set()
        await run_worker(
            store=SecretStore(),
            database_url=migrated,
            stop=stop,
            holder=ME,
        )

    asyncio.run(run())
    assert lease_row(migrated) is None


async def test_an_idle_cycle_asks_to_run_again_within_a_minute(migrated: str) -> None:
    engine = create_engine_for(migrated)
    poller = Poller(
        infra=InfraConfig(),
        sessions=make_session_factory(engine),
        adapters={},
        notifier=None,
    )
    assert await run_scheduled_cycle(poller, "watchlist", ("greenhouse",)) == IDLE_RECHECK_SECONDS


# --- the worker thread and `up` -------------------------------------------------------------


def test_the_worker_thread_starts_and_stops(migrated: str) -> None:
    ended: list[BaseException | None] = []
    worker = WorkerThread(
        store=SecretStore(),
        database_url=migrated,
        holder=ME,
        on_exit=ended.append,
    )
    worker.start()
    deadline = time.monotonic() + 5
    while lease_row(migrated) is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (row := lease_row(migrated)) is not None
    assert row.holder == str(ME)
    worker.stop(timeout=5)
    assert ended == [None]
    assert worker.error is None
    assert lease_row(migrated) is None


class FakeServer:
    """uvicorn.Server's part `run_app` uses: the app's lifespan runs until should_exit."""

    def __init__(self, config: Any) -> None:
        self.app = config.app
        self.should_exit = False

    def run(self) -> None:
        with TestClient(self.app):
            deadline = time.monotonic() + 5
            while not self.should_exit and time.monotonic() < deadline:
                time.sleep(0.02)
        assert self.should_exit


@pytest.fixture
def fake_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobpulse.up.uvicorn.Server", FakeServer)


def test_a_stalled_worker_makes_up_exit_with_code_1(
    db_url: str, fake_server: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()

    async def stalls(**kwargs: Any) -> None:
        started.set()
        raise WorkerStalled("no run of watchlist finished in 90 min")

    monkeypatch.setattr("jobpulse.up.run_worker", stalls)
    code = run_app(store=SecretStore(), database_url=db_url, host="127.0.0.1", port=8000)
    assert started.is_set()
    assert code == 1
    assert lease_row(db_url) is None


def test_up_refuses_while_another_worker_holds_the_lease(migrated: str) -> None:
    with session_scope(open_database(migrated)) as session:
        lease.acquire(session, OTHER, now=utcnow())
    with pytest.raises(lease.LeaseHeld):
        run_app(store=SecretStore(), database_url=migrated, host="127.0.0.1", port=8000)


def test_up_warns_about_an_open_dashboard_only_without_a_password(
    db_url: str,
    fake_server: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="jobpulse.up")
    monkeypatch.setattr("jobpulse.up.run_worker", stalls_once := _stalls())
    run_app(store=SecretStore(), database_url=db_url, host="0.0.0.0", port=8000)
    assert "listens on 0.0.0.0 with no password" in caplog.text
    assert stalls_once.calls == 1
    caplog.clear()
    with session_scope(open_database(db_url)) as session:
        set_password(session, "a-long-pass-1", now=utcnow())
    run_app(store=SecretStore(), database_url=db_url, host="0.0.0.0", port=8000)
    assert "no password" not in caplog.text
    assert "(password on)" in caplog.text


class _stalls:
    """A run_worker that ends at once, so run_app returns."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, **kwargs: Any) -> None:
        self.calls += 1
        raise WorkerStalled("stop")


def test_the_up_command_takes_the_data_folder_host_and_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_run_app(**kwargs: Any) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr("jobpulse.cli.run_app", fake_run_app)
    data = tmp_path / "somewhere"
    result = CliRunner().invoke(
        app, ["--data-dir", str(data), "up", "--host", "0.0.0.0", "--port", "9000"]
    )
    assert result.exit_code == 0, result.output
    assert seen["database_url"] == f"sqlite:///{data / 'jobpulse.db'}"
    assert (seen["host"], seen["port"], seen["data_dir"]) == ("0.0.0.0", 9000, data)
    assert seen["migrate_first"] is True


async def test_the_worker_rebuilds_its_schedule_when_sources_change(
    migrated: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="jobpulse.runtime")
    sessions = open_database(migrated)
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_worker(
            store=SecretStore(),
            database_url=migrated,
            stop=stop,
            holder=ME,
            settings_check_seconds=0.02,
        )
    )

    async def logged(text: str) -> None:
        for _ in range(250):
            if text in caplog.text:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"never logged {text!r}")

    await logged("watchlist every 20 min")
    with session_scope(sessions) as session:  # a profile change that isn't a source setting
        profile_db.save(session, Profile(timezone="America/Chicago"), now=utcnow())
    await asyncio.sleep(0.1)
    assert "rebuilding the schedule" not in caplog.text
    with session_scope(sessions) as session:
        profile_db.save(
            session,
            Profile(sources=SourceSettings(check_every_minutes=45, simplify_feed=False)),
            now=utcnow(),
        )
    await logged("rebuilding the schedule")
    await logged("watchlist every 45 min")
    assert "simplify every" not in caplog.text.split("rebuilding the schedule")[1]
    stop.set()
    await asyncio.wait_for(task, 5)
    assert lease_row(migrated) is None
