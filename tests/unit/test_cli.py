from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobpulse.cli import app

runner = CliRunner()
GOOD_TOPIC = "jobpulse-Zq3vX9kR2mT7wL4pB8nC1dF6"


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for name in ("poll", "worker", "web", "companies", "digest", "db", "config"):
        assert name in result.output


def test_config_check_passes_for_valid_files(tmp_path: Path) -> None:
    config = write(tmp_path / "config.yaml", "contact_email: dev@example.com\n")
    env = write(tmp_path / ".env", f"NTFY_TOPIC={GOOD_TOPIC}\n")
    result = runner.invoke(
        app, ["--config", str(config), "--env-file", str(env), "config", "check"]
    )
    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert GOOD_TOPIC not in result.output


def test_config_check_reports_field_errors(tmp_path: Path) -> None:
    config = write(
        tmp_path / "config.yaml",
        "contact_email: dev@example.com\nwatchlist:\n  - {name: A, ats: nope, token: a}\n",
    )
    env = write(tmp_path / ".env", "")
    result = runner.invoke(
        app, ["--config", str(config), "--env-file", str(env), "config", "check"]
    )
    assert result.exit_code == 2
    assert "watchlist[0].ats" in result.output


def test_config_check_reports_a_missing_secret(tmp_path: Path) -> None:
    config = write(tmp_path / "config.yaml", "contact_email: dev@example.com\n")
    env = write(tmp_path / ".env", "")
    result = runner.invoke(
        app, ["--config", str(config), "--env-file", str(env), "config", "check"]
    )
    assert result.exit_code == 2
    assert "NTFY_TOPIC" in result.output


def test_config_check_prints_placeholder_warnings(tmp_path: Path) -> None:
    example = Path(__file__).resolve().parents[2] / "config.example.yaml"
    env = write(tmp_path / ".env", f"NTFY_TOPIC={GOOD_TOPIC}\n")
    result = runner.invoke(
        app, ["--config", str(example), "--env-file", str(env), "config", "check"]
    )
    assert result.exit_code == 0, result.output
    assert "warning" in result.output.lower()
    assert "<SET ME>" in result.output


@pytest.mark.parametrize(("command", "option"), [("web", "--host"), ("digest", "--dry-run")])
def test_m3_commands_replace_their_placeholders(command: str, option: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output
    assert option in result.output
    assert "arrives in" not in result.output
