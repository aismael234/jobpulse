"""M6 from the command line: `profile import` brings an old config.yaml's preferences,
watchlist and blocked names into the database (once; again changes nothing), `profile
export`/`show` read it back, and `config check` covers config.yaml, the profile and the
secrets."""

import textwrap
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from typer.testing import CliRunner

from jobpulse.cli import app
from jobpulse.config import load_config

runner = CliRunner()
GOOD_TOPIC = "jobpulse-Zq3vX9kR2mT7wL4pB8nC1dF6"
REPO_ROOT = Path(__file__).resolve().parents[2]

LEGACY = """\
contact_email: dev@example.com
timezone: America/Los_Angeles
detection: {freshness_hours: 24}
dashboard: {port: 8100}
filters:
  title:
    include: ['\\bbackend\\b']
notify: {channels: [ntfy]}
watchlist:
  - {name: Acme, ats: greenhouse, token: acme, priority: 5}
  - {name: Old, ats: lever, token: old, active: false}
blocklist: {companies: [Jobot, Revature], boards: [greenhouse/spacex]}
"""


def write(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class Cli:
    def __init__(self, tmp_path: Path, db_url: str) -> None:
        self.tmp_path = tmp_path
        self.db_url = db_url
        self.config = write(tmp_path / "config.yaml", "")
        self.env = write(tmp_path / ".env", "")

    def run(self, *args: str, config: Path | None = None) -> tuple[int, str]:
        base = ["--config", str(config or self.config), "--env-file", str(self.env)]
        result = runner.invoke(app, [*base, *args])
        return result.exit_code, result.output

    def ok(self, *args: str, config: Path | None = None) -> str:
        code, output = self.run(*args, config=config)
        assert code == 0, output
        return output

    def rows(self, query: str) -> list[tuple[object, ...]]:
        with create_engine(self.db_url).connect() as connection:
            return [tuple(row) for row in connection.execute(text(query))]


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Cli:
    db_url = f"sqlite:///{tmp_path / 'jobpulse.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    cli = Cli(tmp_path, db_url)
    cli.ok("db", "upgrade")
    return cli


def test_an_old_config_yaml_is_refused_with_one_line_per_moved_key(cli: Cli) -> None:
    old = write(cli.tmp_path / "old.yaml", LEGACY)
    code, output = cli.run("config", "check", config=old)
    assert code == 2
    for key in ("contact_email", "filters", "notify", "watchlist", "blocklist"):
        assert f"  - {key}: moved to the database" in output
    assert "  - dashboard" not in output  # infrastructure stays valid there


def test_db_upgrade_still_reads_an_old_config_yaml(tmp_path: Path) -> None:
    """Upgrading comes before the import, so it must work with the old file."""
    db = tmp_path / "old-config.db"
    old = write(tmp_path / "old.yaml", LEGACY + f"database_url: sqlite:///{db}\n")
    result = runner.invoke(
        app, ["--config", str(old), "--env-file", str(tmp_path / ".env"), "db", "upgrade"]
    )
    assert result.exit_code == 0, result.output
    assert db.exists()


def test_profile_import_brings_preferences_watchlist_and_blocks_in(cli: Cli) -> None:
    old = write(cli.tmp_path / "old.yaml", LEGACY)
    infra = cli.tmp_path / "config.infra.yaml"
    output = cli.ok("profile", "import", str(old), "--write-infra", str(infra))
    assert "profile: imported from" in output
    assert "watchlist: 2 boards added" in output
    assert "blocklist: 2 companies newly blocked on the dashboard (0 already were)" in output
    companies = cli.rows("select token, active, source_of_discovery from companies order by token")
    assert companies == [("acme", 1, "dashboard"), ("old", 0, "dashboard")]
    assert cli.rows("select name from blocked_companies order by name") == [
        ("Jobot",),
        ("Revature",),
    ]
    exported = cli.ok("profile", "export")
    assert "timezone: America/Los_Angeles" in exported
    assert "greenhouse/spacex" in exported  # boards stay in the profile
    assert "Jobot" not in exported  # names became dashboard blocks
    assert "freshness_hours" not in exported  # infrastructure stays in config.yaml

    written = load_config(infra)  # infrastructure only: config.yaml from now on
    assert written.detection.freshness_hours == 24
    assert written.dashboard.port == 8100


def test_importing_again_changes_nothing(cli: Cli) -> None:
    old = write(cli.tmp_path / "old.yaml", LEGACY)
    cli.ok("profile", "import", str(old))
    output = cli.ok("profile", "import", str(old))
    assert "profile: unchanged (revision 1)" in output
    assert "watchlist: 0 boards added, 0 updated" in output
    assert "(2 already were)" in output
    assert cli.rows("select revision from profile") == [(1,)]


def test_a_different_profile_needs_replace(cli: Cli) -> None:
    cli.ok("profile", "import", str(write(cli.tmp_path / "old.yaml", LEGACY)))
    other = write(cli.tmp_path / "other.yaml", LEGACY.replace("Los_Angeles", "Chicago"))
    code, output = cli.run("profile", "import", str(other))
    assert code == 2
    assert "--replace" in output
    assert "revision 2" in cli.ok("profile", "import", str(other), "--replace")
    assert "America/Chicago" in cli.ok("profile", "export")


def test_config_owned_companies_become_dashboard_rows(cli: Cli) -> None:
    with create_engine(cli.db_url).begin() as connection:
        connection.execute(
            text(
                "insert into companies (name, ats_type, token, region, priority, active, "
                "source_of_discovery, created_at, updated_at) values ('Beta', 'lever', 'beta', "
                "'us', 0, 0, 'config', '2026-10-01 00:00:00', '2026-10-01 00:00:00')"
            )
        )
    cli.ok("profile", "import", str(write(cli.tmp_path / "old.yaml", LEGACY)))
    assert cli.rows("select source_of_discovery from companies where token = 'beta'") == [
        ("dashboard",)
    ]


def test_write_infra_never_overwrites(cli: Cli) -> None:
    old = write(cli.tmp_path / "old.yaml", LEGACY)
    code, output = cli.run("profile", "import", str(old), "--write-infra", str(cli.config))
    assert code == 2
    assert "exists" in output
    assert cli.rows("select count(*) from profile") == [(0,)]


def test_profile_show_summarizes_it(cli: Cli) -> None:
    assert "no profile stored" in cli.ok("profile", "show")
    cli.ok("profile", "import", str(write(cli.tmp_path / "old.yaml", LEGACY)))
    output = cli.ok("profile", "show")
    assert "revision 1" in output
    assert "onboarded" in output
    assert "title rules: 1 include" in output
    assert "channels: ntfy" in output


def test_commands_that_poll_need_a_profile(cli: Cli) -> None:
    code, output = cli.run("poll", "--dry-run")
    assert code == 2
    assert "jobpulse profile import" in output
    code, output = cli.run("config", "check")
    assert code == 2
    assert "jobpulse profile import" in output


def test_config_check_passes_with_a_secret_in_env(cli: Cli) -> None:
    cli.ok("profile", "import", str(write(cli.tmp_path / "old.yaml", LEGACY)))
    write(cli.env, f"NTFY_TOPIC={GOOD_TOPIC}\n")
    output = cli.ok("config", "check")
    assert "OK; profile revision 1 (2 companies, 0 searches; channels: ntfy)" in output
    assert GOOD_TOPIC not in output


def test_config_check_reports_a_missing_secret(cli: Cli) -> None:
    cli.ok("profile", "import", str(write(cli.tmp_path / "old.yaml", LEGACY)))
    code, output = cli.run("config", "check")
    assert code == 2
    assert "NTFY_TOPIC" in output
    assert "Settings" in output


def test_a_secret_saved_on_settings_satisfies_config_check(cli: Cli) -> None:
    cli.ok("profile", "import", str(write(cli.tmp_path / "old.yaml", LEGACY)))
    with create_engine(cli.db_url).begin() as connection:
        connection.execute(
            text(
                "insert into secrets (name, value, updated_at) "
                "values ('ntfy_topic', :topic, '2026-11-01 00:00:00')"
            ),
            {"topic": GOOD_TOPIC},
        )
    output = cli.ok("config", "check")
    assert "OK" in output
    assert GOOD_TOPIC not in output


def test_config_check_prints_placeholder_warnings(cli: Cli) -> None:
    profile = (REPO_ROOT / "profile.example.yaml").read_text(encoding="utf-8")
    old = write(cli.tmp_path / "example.yaml", profile)
    cli.ok("profile", "import", str(old))
    write(cli.env, f"NTFY_TOPIC={GOOD_TOPIC}\n")
    output = cli.ok("config", "check", config=REPO_ROOT / "config.example.yaml")
    assert "warning" in output.lower()
    assert "<SET ME>" in output


def test_an_explicit_config_file_must_exist(cli: Cli) -> None:
    code, output = cli.run("config", "check", config=cli.tmp_path / "nope.yaml")
    assert code == 2
    assert "not found" in output
