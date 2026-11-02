"""M7 from the command line: `companies seed apply` (dry run first), `companies seed export`
(agencies and dead boards out, tombstones kept, --add validated) and `companies contribute`."""

import json
import textwrap
from pathlib import Path

import pytest
import respx
from sqlalchemy import create_engine, text
from typer.testing import CliRunner

from jobpulse.cli import app
from jobpulse.seed import Seed, SeedBoard, packaged_seed, parse_seed, render_seed
from jobpulse.sources.greenhouse import GreenhouseAdapter
from jobpulse.sources.lever import LeverAdapter

pytestmark = pytest.mark.respx(assert_all_called=False)

runner = CliRunner()
PROFILE = """\
contact_email: dev@example.com
blocklist: {boards: [greenhouse/spacex]}
"""


class Cli:
    def __init__(self, tmp_path: Path, db_url: str) -> None:
        self.tmp_path = tmp_path
        self.db_url = db_url
        self.config = tmp_path / "config.yaml"
        self.config.write_text("", encoding="utf-8")
        self.env = tmp_path / ".env"
        self.env.write_text("", encoding="utf-8")

    def run(self, *args: str) -> tuple[int, str, str]:
        base = ["--config", str(self.config), "--env-file", str(self.env)]
        result = runner.invoke(app, [*base, *args])
        return result.exit_code, result.stdout, result.stderr

    def ok(self, *args: str) -> tuple[str, str]:
        code, out, err = self.run(*args)
        assert code == 0, out + err
        return out, err

    def sql(self, *statements: str) -> None:
        with create_engine(self.db_url).begin() as connection:
            for statement in statements:
                connection.execute(text(statement))

    def rows(self, query: str) -> list[tuple[object, ...]]:
        with create_engine(self.db_url).connect() as connection:
            return [tuple(row) for row in connection.execute(text(query))]

    def company(self, ats: str, token: str, name: str, **values: object) -> None:
        columns = {
            "name": name,
            "ats_type": ats,
            "token": token,
            "region": "us",
            "priority": 0,
            "active": 1,
            "source_of_discovery": "dashboard",
            "created_at": "2026-10-30 00:00:00",
            "updated_at": "2026-10-30 00:00:00",
            **values,
        }
        names = ", ".join(columns)
        values_sql = ", ".join(f":{key}" for key in columns)
        with create_engine(self.db_url).begin() as connection:
            connection.execute(
                text(f"INSERT INTO companies ({names}) VALUES ({values_sql})"), columns
            )

    def run_row(self, ats: str, token: str, *, ok: bool, kind: str | None, at: str) -> None:
        with create_engine(self.db_url).begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO runs (source, target, started_at, ok, fetched_count, new_count, "
                    "error_kind, not_modified) VALUES (:s, :t, :at, :ok, 0, 0, :kind, 0)"
                ),
                {"s": ats, "t": token, "at": at, "ok": ok, "kind": kind},
            )


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Cli:
    db_url = f"sqlite:///{tmp_path / 'jobpulse.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cli = Cli(tmp_path, db_url)
    cli.ok("db", "upgrade")
    profile = tmp_path / "profile.yaml"
    profile.write_text(textwrap.dedent(PROFILE), encoding="utf-8")
    cli.ok("profile", "import", str(profile))
    return cli


def write_seed(path: Path, *boards: SeedBoard, removed: tuple[tuple[str, str], ...] = ()) -> Path:
    path.write_text(render_seed(Seed(boards, removed)), encoding="utf-8")
    return path


def test_apply_says_what_it_would_do_then_does_it_once(cli: Cli) -> None:
    cli.company("greenhouse", "acme", "Acme")
    seed = write_seed(
        cli.tmp_path / "seed.json",
        SeedBoard("greenhouse", "acme", "Acme"),
        SeedBoard("greenhouse", "newco", "NewCo"),
        SeedBoard("greenhouse", "spacex", "SpaceX"),
        SeedBoard("ashby", "people-culture-talent", "People Culture Talent"),
    )
    out, _ = cli.ok("companies", "seed", "apply", "--file", str(seed), "--dry-run")
    assert "would add greenhouse/newco" in out
    assert "skipped greenhouse/spacex (SpaceX), blocked: on your blocklist" in out
    assert "skipped ashby/people-culture-talent (People Culture Talent), blocked" in out
    assert (
        "4 boards in the seed: 1 to add, 1 already on your watchlist, 2 blocked, 0 left out" in out
    )
    assert cli.rows("select token from companies order by token") == [("acme",)]

    out, _ = cli.ok("companies", "seed", "apply", "--file", str(seed))
    assert "add greenhouse/newco" in out
    assert "Their first poll stores the jobs they have now without notifying you." in out
    assert cli.rows("select token, source_of_discovery from companies order by token") == [
        ("acme", "dashboard"),
        ("newco", "seed"),
    ]
    out, _ = cli.ok("companies", "seed", "apply", "--file", str(seed))
    assert "0 added, 2 already on your watchlist" in out


def test_apply_refuses_a_malformed_seed_file(cli: Cli) -> None:
    bad = cli.tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": 1, "boards": [{"ats": "greenhouse"}]}), "utf-8")
    code, out, err = cli.run("companies", "seed", "apply", "--file", str(bad))
    assert code != 0
    assert "boards[0].token" in out + err


def test_the_packaged_seed_applies_to_an_empty_watchlist(
    cli: Cli, respx_mock: respx.MockRouter
) -> None:
    """Only Workday boards are searched (for their keywords); every other board goes in as
    the file names it."""
    searches = respx_mock.post(url__regex=r"https://[^/]+\.myworkdayjobs\.com/wday/cxs/").respond(
        json={"total": 3, "jobPostings": []}
    )
    seed = packaged_seed()
    blocked = sum(1 for board in seed.boards if board.label == "greenhouse/spacex")
    workday = sum(1 for board in seed.boards if board.ats == "workday")

    out, _ = cli.ok("companies", "seed", "apply")

    assert f"{len(seed.boards) - blocked:,} added, 0 already on your watchlist" in out
    assert searches.call_count == workday * 4
    (count,) = cli.rows("select count(*) from companies where source_of_discovery = 'seed'")
    assert count == (len(seed.boards) - blocked,)


def test_export_builds_the_seed_from_the_watchlist(cli: Cli, respx_mock: respx.MockRouter) -> None:
    cli.company("greenhouse", "stripe", "Stripe", priority=7, source_of_discovery="auto")
    cli.company(
        "workday",
        "postman.wd108/careers",
        "Postman",
        params=json.dumps({"keywords": ["software engineer"]}),
    )
    cli.company("lever", "olx", "OLX", region="eu", source_of_discovery="import")
    cli.company("ashby", "talenthop", "TalentHop")  # a staffing agency
    cli.company("greenhouse", "gone", "Gone")
    cli.run_row("greenhouse", "gone", ok=True, kind=None, at="2026-10-30 00:00:00")
    cli.run_row("greenhouse", "gone", ok=False, kind="http_4xx", at="2026-10-31 00:00:00")
    cli.company("greenhouse", "flaky", "Flaky")
    cli.run_row("greenhouse", "flaky", ok=False, kind="http_4xx", at="2026-10-30 00:00:00")
    cli.run_row("greenhouse", "flaky", ok=True, kind=None, at="2026-10-31 00:00:00")
    cli.company("greenhouse", "paused", "Paused", active=0)
    cli.company("ashby", "acme-recruiting", "Acme Recruiting")
    out_file = write_seed(
        cli.tmp_path / "boards.json",
        SeedBoard("greenhouse", "gone", "Gone"),
        SeedBoard("greenhouse", "elsewhere", "Elsewhere"),  # not here: kept
        removed=(("ashby", "old"),),
    )
    respx_mock.get(GreenhouseAdapter.list_url("anthropic")).respond(
        json={"jobs": [{"id": 1, "title": "Engineer", "company_name": "Anthropic"}]}
    )
    respx_mock.get(LeverAdapter.list_url("palantir")).respond(json=[{"id": "a"}])
    respx_mock.get(GreenhouseAdapter.list_url("nope")).respond(404, json={})

    _, err = cli.ok(
        "companies", "seed", "export", "--out", str(out_file),
        "--add", "greenhouse/anthropic",
        "--add", "lever/palantir=Palantir",
        "--add", "greenhouse/nope",
        "--add", "greenhouse/stripe",
    )  # fmt: skip

    seed = parse_seed(out_file.read_text(encoding="utf-8"))
    assert {(b.label, b.name, b.region) for b in seed.boards} == {
        ("ashby/acme-recruiting", "Acme Recruiting", "us"),
        ("greenhouse/anthropic", "Anthropic", "us"),
        ("greenhouse/elsewhere", "Elsewhere", "us"),
        ("greenhouse/flaky", "Flaky", "us"),
        ("greenhouse/stripe", "Stripe", "us"),
        ("lever/olx", "OLX", "eu"),
        ("lever/palantir", "Palantir", "us"),
        ("workday/postman.wd108/careers", "Postman", "us"),
    }
    assert set(seed.removed) == {("ashby", "old"), ("greenhouse", "gone")}
    assert "priority" not in out_file.read_text()
    assert "keywords" not in out_file.read_text()
    assert "left out (staffing agency): ashby/talenthop (TalentHop)" in err
    assert "left out (latest run a 4xx): greenhouse/gone (Gone)" in err
    assert "couldn't validate greenhouse/nope: HTTP 404" in err
    assert "already in the seed: greenhouse/stripe" in err
    assert "review (looks like an agency): ashby/acme-recruiting (Acme Recruiting)" in err
    assert "8 boards and 2 removed in the seed (1 kept from the previous seed" in err


def test_contribute_writes_the_boards_the_seed_lacks(cli: Cli) -> None:
    seed = packaged_seed()
    known = seed.boards[0] if seed.boards else None
    if known is not None:
        cli.company(known.ats, known.token, known.name)
    cli.company("greenhouse", "zz-mine", "Mine")
    cli.company("greenhouse", "zz-seeded", "Seeded", source_of_discovery="seed")
    cli.company("greenhouse", "zz-paused", "Paused", active=0)
    cli.company("ashby", "nova-talent", "Nova Talent")
    out_file = cli.tmp_path / "mine.json"

    _, err = cli.ok("companies", "contribute", "--out", str(out_file))

    mine = parse_seed(out_file.read_text(encoding="utf-8"))
    assert [b.label for b in mine.boards] == ["greenhouse/zz-mine"]
    assert "1 board the seed lacks in" in err
    assert "Open a pull request." in err

    cli.sql("UPDATE companies SET active = 0 WHERE token = 'zz-mine'")
    code, out, err = cli.run("companies", "contribute")
    assert code == 0
    assert out == ""
    assert "nothing to contribute" in err
