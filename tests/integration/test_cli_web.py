"""``jobpulse web`` from the command line."""

from pathlib import Path
from typing import Any

import pytest
import time_machine
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from jobpulse.cli import app
from tests.factories import NOW

runner = CliRunner()
CONFIG = """\
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
