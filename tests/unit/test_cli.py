from pathlib import Path

from typer.testing import CliRunner

from jobpulse.cli import app

runner = CliRunner()


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for name in ("up", "poll", "companies", "db", "profile", "resume", "password", "--data-dir"):
        assert name in result.output
    assert "digest" not in result.output  # every match is pushed; there's no digest
    for gone in ("worker", "web ", "config", "--env-file"):
        assert gone not in result.output


def test_up_takes_a_host_and_port() -> None:
    result = runner.invoke(app, ["up", "--help"])
    assert result.exit_code == 0, result.output
    assert "--host" in result.output
    assert "--port" in result.output
