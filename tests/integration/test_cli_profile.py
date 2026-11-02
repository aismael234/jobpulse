"""The command line without a config file: `--data-dir` names the database's folder,
`profile export`/`import` back the profile up and restore it, `profile show` summarizes it,
`notify test` names the secret Settings still needs, and `password clear` gets you back in."""

import textwrap
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from typer.testing import CliRunner

from jobpulse.cli import app

runner = CliRunner()
GOOD_TOPIC = "jobpulse-Zq3vX9kR2mT7wL4pB8nC1dF6"

PROFILE = """\
contact_email: dev@example.com
timezone: America/Los_Angeles
filters:
  title:
    include: ['\\bbackend\\b']
notify: {channels: [ntfy]}
blocklist: {boards: [greenhouse/spacex]}
"""


def write(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class Cli:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.data = tmp_path / "data"
        self.db_url = f"sqlite:///{self.data / 'jobpulse.db'}"

    def run(self, *args: str) -> tuple[int, str]:
        result = runner.invoke(app, ["--data-dir", str(self.data), *args])
        return result.exit_code, result.output

    def ok(self, *args: str) -> str:
        code, output = self.run(*args)
        assert code == 0, output
        return output

    def stdout(self, *args: str) -> str:
        """What a command prints for a file (log lines go to stderr)."""
        result = runner.invoke(app, ["--data-dir", str(self.data), *args])
        assert result.exit_code == 0, result.output
        return result.stdout

    def rows(self, query: str) -> list[tuple[object, ...]]:
        with create_engine(self.db_url).connect() as connection:
            return [tuple(row) for row in connection.execute(text(query))]

    def execute(self, statement: str, **params: object) -> None:
        with create_engine(self.db_url).begin() as connection:
            connection.execute(text(statement), params)


@pytest.fixture
def cli(tmp_path: Path) -> Cli:
    cli = Cli(tmp_path)
    cli.ok("db", "upgrade")
    return cli


def test_the_database_lives_in_the_data_folder(cli: Cli) -> None:
    assert (cli.data / "jobpulse.db").exists()
    assert cli.rows("select version_num from alembic_version") == [("0011",)]


@pytest.mark.parametrize("command", ["worker", "web", "config"])
def test_the_separate_commands_and_config_files_are_gone(cli: Cli, command: str) -> None:
    code, _ = cli.run(command)
    assert code == 2
    for option in ("--config", "--env-file"):
        assert runner.invoke(app, [option, "x", "profile", "show"]).exit_code == 2


def test_a_profile_round_trips_through_export_and_import(cli: Cli) -> None:
    saved = write(cli.tmp_path / "profile.yaml", PROFILE)
    assert "profile: imported from" in cli.ok("profile", "import", str(saved))
    exported = cli.stdout("profile", "export")
    assert "timezone: America/Los_Angeles" in exported
    assert "greenhouse/spacex" in exported
    assert "sources:" in exported  # Settings → Sources & schedule travels with it
    backup = write(cli.tmp_path / "backup.yaml", exported)
    assert "profile: unchanged (revision 1)" in cli.ok("profile", "import", str(backup))
    assert cli.rows("select revision from profile") == [(1,)]


def test_a_different_profile_needs_replace(cli: Cli) -> None:
    cli.ok("profile", "import", str(write(cli.tmp_path / "a.yaml", PROFILE)))
    other = write(cli.tmp_path / "b.yaml", PROFILE.replace("Los_Angeles", "Chicago"))
    code, output = cli.run("profile", "import", str(other))
    assert code == 2
    assert "--replace" in output
    assert "revision 2" in cli.ok("profile", "import", str(other), "--replace")
    assert "America/Chicago" in cli.ok("profile", "export")


def test_an_invalid_profile_names_the_field(cli: Cli) -> None:
    bad = write(cli.tmp_path / "bad.yaml", "timezone: Mars/Olympus_Mons\n")
    code, output = cli.run("profile", "import", str(bad))
    assert code == 2
    assert "timezone" in output
    assert cli.rows("select count(*) from profile") == [(0,)]


def test_profile_show_summarizes_it(cli: Cli) -> None:
    assert "no profile stored" in cli.ok("profile", "show")
    cli.ok("profile", "import", str(write(cli.tmp_path / "p.yaml", PROFILE)))
    output = cli.ok("profile", "show")
    assert "revision 1" in output
    assert "onboarded" in output
    assert "title rules: 1 include" in output
    assert "channels: ntfy" in output


def test_commands_that_poll_need_a_profile(cli: Cli) -> None:
    code, output = cli.run("poll", "--dry-run")
    assert code == 2
    assert "setup questions" in output


def test_notify_test_names_the_secret_settings_still_needs(cli: Cli) -> None:
    cli.ok("profile", "import", str(write(cli.tmp_path / "p.yaml", PROFILE)))
    code, output = cli.run("notify", "test")
    assert code == 2
    assert "ntfy topic" in output
    assert "Settings" in output


def test_password_clear_gets_you_back_in(cli: Cli) -> None:
    assert "the dashboard has no password" in cli.ok("password", "clear")
    cli.execute(
        "insert into secrets (name, value, updated_at) "
        "values ('dashboard_password', 'scrypt$c2FsdA==$aGFzaA==', '2026-11-01 00:00:00')"
    )
    assert "dashboard password removed" in cli.ok("password", "clear")
    assert cli.rows("select count(*) from secrets") == [(0,)]
