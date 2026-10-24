"""``jobpulse web`` and ``jobpulse digest`` from the command line."""

from pathlib import Path
from typing import Any

import pytest
import respx
import time_machine
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from jobpulse.cli import app
from jobpulse.db.tables import PostingRow
from tests.factories import NOW, posting_row

runner = CliRunner()
TOPIC = "jobpulse-Zq3vX9kR2mT7wL4pB8nC1dF6"
CONFIG = """\
contact_email: dev@example.com
notify:
  channels: [ntfy]
  dashboard_url: https://jobpulse.tail1234.ts.net/
simplify: {enabled: false}
discovery: {enabled: false}
"""


class Cli:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config = tmp_path / "config.yaml"
        self.config.write_text(CONFIG, encoding="utf-8")
        self.env = tmp_path / ".env"
        self.env.write_text("", encoding="utf-8")
        self.db_url = f"sqlite:///{tmp_path / 'jobpulse.db'}"
        monkeypatch.setenv("DATABASE_URL", self.db_url)

    def __call__(self, *args: str) -> Any:
        return runner.invoke(
            app, ["--config", str(self.config), "--env-file", str(self.env), *args]
        )

    def upgrade(self) -> None:
        result = self("db", "upgrade")
        assert result.exit_code == 0, result.output

    def add(self, **fields: Any) -> None:
        with Session(create_engine(self.db_url)) as session:
            session.add(posting_row(**fields))
            session.commit()

    def digest_marks(self) -> list[Any]:
        with Session(create_engine(self.db_url)) as session:
            return list(session.scalars(select(PostingRow.digest_sent_at).order_by(PostingRow.id)))


@pytest.fixture
def cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, time_machine: time_machine.TimeMachineFixture
) -> Cli:
    time_machine.move_to(NOW, tick=False)
    return Cli(tmp_path, monkeypatch)


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """What ``jobpulse web`` hands to uvicorn, instead of serving it."""
    calls: dict[str, Any] = {}

    def fake_run(application: Any, **options: Any) -> None:
        calls["app"] = application
        calls.update(options)

    monkeypatch.setattr("uvicorn.run", fake_run)
    return calls


def test_web_serves_the_dashboard_on_localhost_by_default(cli: Cli, served: dict[str, Any]) -> None:
    cli.upgrade()
    result = cli("web")
    assert result.exit_code == 0, result.output
    assert (served["host"], served["port"]) == ("127.0.0.1", 8000)
    assert isinstance(served["app"], FastAPI)
    with TestClient(served["app"]) as client:
        assert client.get("/healthz").text == "ok"
        assert client.get("/").status_code == 200


def test_web_options_override_the_bind_address_and_warn_without_a_password(
    cli: Cli, served: dict[str, Any]
) -> None:
    cli.upgrade()
    result = cli("web", "--host", "0.0.0.0", "--port", "9000")
    assert result.exit_code == 0, result.output
    assert (served["host"], served["port"]) == ("0.0.0.0", 9000)
    assert "no password" in result.output


def test_web_turns_on_basic_auth_from_the_env_file(cli: Cli, served: dict[str, Any]) -> None:
    cli.upgrade()
    cli.env.write_text("DASHBOARD_USER=me\nDASHBOARD_PASSWORD=s3cret-pass-29\n", encoding="utf-8")
    result = cli("web", "--host", "0.0.0.0")
    assert result.exit_code == 0, result.output
    assert "no password" not in result.output
    assert "s3cret-pass-29" not in result.output
    with TestClient(served["app"]) as client:
        assert client.get("/").status_code == 401
        assert client.get("/", auth=("me", "s3cret-pass-29")).status_code == 200


def test_web_refuses_half_configured_auth(cli: Cli, served: dict[str, Any]) -> None:
    cli.upgrade()
    cli.env.write_text("DASHBOARD_USER=me\n", encoding="utf-8")
    result = cli("web")
    assert result.exit_code == 2
    assert "DASHBOARD_PASSWORD" in result.output
    assert served == {}


def test_web_needs_a_migrated_database(cli: Cli, served: dict[str, Any]) -> None:
    result = cli("web")
    assert result.exit_code == 2
    assert "jobpulse db upgrade" in result.output
    assert served == {}


def test_digest_dry_run_prints_the_message_and_marks_nothing(cli: Cli) -> None:
    cli.upgrade()
    cli.add(title="Backend Engineer", score=0.47)

    result = cli("digest", "--dry-run")  # no NTFY_TOPIC needed

    assert result.exit_code == 0, result.output
    assert "dry run" in result.output
    assert "jobpulse digest · 1 new match" in result.output
    assert "1. Acme · Backend Engineer · SF · posted 2 h ago (score 0.47)" in result.output
    assert cli.digest_marks() == [None]


def test_digest_needs_the_notification_secrets(cli: Cli) -> None:
    cli.upgrade()
    result = cli("digest")
    assert result.exit_code == 2
    assert "NTFY_TOPIC" in result.output


def test_digest_with_nothing_to_send(cli: Cli) -> None:
    cli.upgrade()
    cli.env.write_text(f"NTFY_TOPIC={TOPIC}\n", encoding="utf-8")
    result = cli("digest")
    assert result.exit_code == 0, result.output
    assert "nothing to send" in result.output


def test_digest_sends_one_ntfy_message(cli: Cli, respx_mock: respx.MockRouter) -> None:
    cli.upgrade()
    cli.add(title="Backend Engineer", score=0.47)
    cli.add(title="Platform Engineer", score=0.41)
    cli.env.write_text(f"NTFY_TOPIC={TOPIC}\n", encoding="utf-8")
    ntfy = respx_mock.post(f"https://ntfy.sh/{TOPIC}").respond(200, json={"id": "x"})

    result = cli("digest")

    assert result.exit_code == 0, result.output
    assert "digest sent: 2 postings" in result.output
    assert TOPIC not in result.output
    assert ntfy.call_count == 1
    request = ntfy.calls[0].request
    assert request.headers["Title"] == "jobpulse digest · 2 new matches"
    assert request.headers["Click"] == "https://jobpulse.tail1234.ts.net/"
    assert "Platform Engineer" in request.content.decode()
    assert all(mark is not None for mark in cli.digest_marks())


def test_a_failed_digest_exits_non_zero(cli: Cli, respx_mock: respx.MockRouter) -> None:
    cli.upgrade()
    cli.add(title="Backend Engineer")
    cli.env.write_text(f"NTFY_TOPIC={TOPIC}\n", encoding="utf-8")
    respx_mock.post(f"https://ntfy.sh/{TOPIC}").respond(403)

    result = cli("digest")

    assert result.exit_code == 1
    assert "ntfy answered HTTP 403" in result.output
    assert cli.digest_marks() == [None]


def test_web_keeps_health_probes_out_of_the_access_log(cli: Cli, served: dict[str, Any]) -> None:
    import logging

    cli.upgrade()
    assert cli("web").exit_code == 0
    access = logging.getLogger("uvicorn.access")

    def record(path: str) -> logging.LogRecord:
        return access.makeRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            0,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:5000", "GET", path, "1.1", 200),
            None,
        )

    assert not access.filter(record("/healthz"))
    assert access.filter(record("/?view=saved"))
