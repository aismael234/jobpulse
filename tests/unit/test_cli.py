import re
from pathlib import Path

from typer.testing import CliRunner

from jobpulse.cli import app

runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def help_text(*args: str) -> str:
    """The help as plain text: Rich colors it where it detects CI (GITHUB_ACTIONS), and the
    escape codes split option names such as --data-dir."""
    result = runner.invoke(app, [*args, "--help"])
    assert result.exit_code == 0, result.output
    return ANSI.sub("", result.output)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_help_lists_every_command() -> None:
    output = help_text()
    for name in ("up", "poll", "companies", "db", "profile", "resume", "password", "--data-dir"):
        assert name in output
    assert "digest" not in output  # every match is pushed; there's no digest
    for gone in ("worker", "web ", "config", "--env-file"):
        assert gone not in output


def test_up_takes_a_host_and_port() -> None:
    output = help_text("up")
    assert "--host" in output
    assert "--port" in output
