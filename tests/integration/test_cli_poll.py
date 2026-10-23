"""M1 acceptance: ``poll --once --dry-run`` over recorded responses prints the expected lines."""

from pathlib import Path

import pytest
import time_machine
from sqlalchemy import create_engine, func, select, table
from typer.testing import CliRunner

from jobpulse.cli import app
from tests.factories import FIXTURES, NOW

SCENARIOS = FIXTURES / "scenarios"
CONFIG = SCENARIOS / "config.yaml"

runner = CliRunner()


def count_rows(db_url: str, name: str) -> int:
    with create_engine(db_url).connect() as connection:
        return int(connection.scalar(select(func.count()).select_from(table(name))) or 0)


@pytest.fixture
def cli(
    tmp_path: Path, time_machine: time_machine.TimeMachineFixture, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str], str]:
    time_machine.move_to(NOW, tick=False)
    db_url = f"sqlite:///{tmp_path / 'jobpulse.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    base = ["--config", str(CONFIG), "--env-file", str(tmp_path / ".env")]
    upgraded = runner.invoke(app, [*base, "db", "upgrade"])
    assert upgraded.exit_code == 0, upgraded.output
    return base, db_url


def poll(base: list[str], scenario: str, *extra: str) -> str:
    result = runner.invoke(
        app, [*base, "poll", "--once", *extra, "--fixtures", str(SCENARIOS / scenario)]
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_dry_run_of_a_first_poll_prints_baseline_lines_and_saves_nothing(
    cli: tuple[list[str], str],
) -> None:
    base, db_url = cli
    output = poll(base, "first_poll", "--dry-run")

    assert "greenhouse/acme: baseline: 5 postings would be stored silently" in output
    assert "lever/beta: baseline: 3 postings would be stored silently" in output
    assert "ashby/gamma: baseline: 3 postings would be stored silently" in output
    assert "would notify: nothing" in output
    for name in ("postings", "runs", "companies"):
        assert count_rows(db_url, name) == 0


def test_dry_run_after_a_baseline_prints_the_push_it_would_send(
    cli: tuple[list[str], str],
) -> None:
    base, db_url = cli
    live = poll(base, "first_poll")
    assert "greenhouse/acme: baseline: stored 5 postings silently" in live
    postings_before = count_rows(db_url, "postings")
    runs_before = count_rows(db_url, "runs")

    output = poll(base, "second_poll", "--dry-run")

    assert (
        "would notify: Acme · Backend Engineer, Platform API · SF / Remote · posted 25 min ago"
        in output
    )
    assert "greenhouse/acme: 6 fetched, 1 new" in output
    assert count_rows(db_url, "postings") == postings_before
    assert count_rows(db_url, "runs") == runs_before


def test_poll_without_a_migrated_database_says_what_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'empty.db'}")
    result = runner.invoke(
        app,
        ["--config", str(CONFIG), "--env-file", str(tmp_path / ".env"), "poll", "--dry-run"],
    )
    assert result.exit_code == 2
    assert "jobpulse db upgrade" in result.output


def test_unknown_source_is_rejected(cli: tuple[list[str], str]) -> None:
    base, _ = cli
    result = runner.invoke(app, [*base, "poll", "--dry-run", "--source", "monster"])
    assert result.exit_code == 2
    assert "monster" in result.output


def test_companies_sync_and_list(cli: tuple[list[str], str]) -> None:
    base, _ = cli
    synced = runner.invoke(app, [*base, "companies", "sync"])
    assert synced.exit_code == 0, synced.output
    assert "added: greenhouse/acme" in synced.output
    listed = runner.invoke(app, [*base, "companies", "list"])
    assert listed.exit_code == 0, listed.output
    assert listed.output.count("baseline pending") == 3
