"""M1 and M2 acceptance: ``poll --once --dry-run`` over recorded responses prints the expected
lines, and a LinkedIn copy of a Greenhouse job is merged rather than pushed."""

from pathlib import Path

import pytest
import time_machine
from sqlalchemy import create_engine, func, select, table
from typer.testing import CliRunner

from jobpulse.cli import app
from tests.factories import FIXTURES, NOW

SCENARIOS = FIXTURES / "scenarios"
CONFIG = SCENARIOS / "config.yaml"  # infrastructure
IMPORT = SCENARIOS / "import.yaml"  # preferences and watchlist, as an old config.yaml had them

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
    imported = runner.invoke(app, [*base, "profile", "import", str(IMPORT)])
    assert imported.exit_code == 0, imported.output
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
    assert "simplify/new-grad: baseline: 7 postings would be stored silently" in output
    assert "linkedin/backend-sf: baseline: 1 posting would be stored silently" in output
    assert (
        "discovery: would add lever/delta (Delta, 2 jobs) from "
        "https://jobs.lever.co/delta/98959f08-1d1b-4b75-919e-833f825394b0/apply"
    ) in output
    # Only Delta has a posting that passes the scenario's title rule; Zeta's page is over the
    # scenario's page cap of 0.
    assert "discovery: 1 more candidate waits for later cycles" in output
    assert "would notify: nothing" in output
    for name in ("postings", "runs", "searches", "source_state"):
        assert count_rows(db_url, name) == 0
    assert count_rows(db_url, "companies") == 3  # the imported watchlist, and nothing more


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
    assert "push (would send): 2 new job postings were found via all sources. Top score: " in output
    assert "greenhouse/acme: 6 fetched, 1 new" in output
    assert count_rows(db_url, "postings") == postings_before
    assert count_rows(db_url, "runs") == runs_before


def test_dry_run_merges_a_linkedin_copy_onto_its_greenhouse_row(
    cli: tuple[list[str], str],
) -> None:
    """M2 acceptance: the copy merges by fingerprint (LinkedIn rows have no job_url_direct)
    and is not pushed; a LinkedIn-only job still is."""
    base, _ = cli
    poll(base, "first_poll")

    output = poll(base, "second_poll", "--dry-run")

    assert "linkedin/backend-sf: 3 fetched, 2 new, 1 merged" in output
    assert (
        "  merged: Acme, Inc. · Backend Engineer – Payments (linkedin 4101100002) "
        "into greenhouse 4012345007 by fingerprint"
    ) in output
    assert "would notify: Northwind · Backend Engineer, Search · Seattle · posted today" in output
    assert "Payments" not in "".join(line for line in output.splitlines() if "notify" in line)
    assert "simplify/new-grad: unchanged since the last fetch (304)" in output
    assert (
        "lever/delta: baseline: 2 postings would be stored silently (1 took over an earlier copy)"
    ) in output


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


def test_a_source_option_polls_only_that_source(cli: tuple[list[str], str]) -> None:
    base, _ = cli
    output = poll(base, "first_poll", "--dry-run", "--source", "simplify")
    assert "simplify/new-grad: baseline: 7 postings would be stored silently" in output
    assert "greenhouse/acme" not in output
    assert "linkedin/backend-sf" not in output


def test_poll_on_an_outdated_schema_says_what_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'old.db'}")
    base = ["--config", str(CONFIG), "--env-file", str(tmp_path / ".env")]
    assert runner.invoke(app, [*base, "db", "upgrade", "0001"]).exit_code == 0
    result = runner.invoke(app, [*base, "poll", "--dry-run"])
    assert result.exit_code == 2
    assert "0001" in result.output
    assert "jobpulse db upgrade" in result.output


def test_unknown_source_is_rejected(cli: tuple[list[str], str]) -> None:
    base, _ = cli
    result = runner.invoke(app, [*base, "poll", "--dry-run", "--source", "monster"])
    assert result.exit_code == 2
    assert "monster" in result.output


def test_companies_list_marks_blocked_companies(cli: tuple[list[str], str], tmp_path: Path) -> None:
    base, _ = cli
    poll(base, "first_poll")  # discovery adds Delta
    blocking = tmp_path / "blocking.yaml"
    blocking.write_text(
        IMPORT.read_text(encoding="utf-8") + "blocklist: {companies: [Delta]}\n", encoding="utf-8"
    )
    imported = runner.invoke(app, [*base, "profile", "import", str(blocking), "--replace"])
    assert imported.exit_code == 0, imported.output
    listed = runner.invoke(app, [*base, "companies", "list"])
    assert listed.exit_code == 0, listed.output
    [delta] = [line for line in listed.output.splitlines() if " delta " in line]
    assert "blocked" in delta
    assert "(auto)" in delta
    assert all("blocked" not in line for line in listed.output.splitlines() if " acme " in line)


def test_the_imported_watchlist_is_listed_and_owned_by_the_dashboard(
    cli: tuple[list[str], str],
) -> None:
    base, _ = cli
    listed = runner.invoke(app, [*base, "companies", "list"])
    assert listed.exit_code == 0, listed.output
    assert listed.output.count("baseline pending") == 3
    assert listed.output.count("(dashboard)") == 3
    assert "sync" not in runner.invoke(app, [*base, "companies", "--help"]).output


def test_workday_is_a_source(cli: tuple[list[str], str]) -> None:
    base, _ = cli
    output = poll(base, "first_poll", "--dry-run", "--source", "workday")
    assert "unknown source" not in output
    assert "would notify: nothing" in output
