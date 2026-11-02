from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobpulse.cli import app

runner = CliRunner()


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for name in ("poll", "worker", "web", "companies", "db", "config", "profile", "resume"):
        assert name in result.output
    assert "digest" not in result.output  # every match is pushed; there's no digest


@pytest.mark.parametrize(("command", "option"), [("web", "--host")])
def test_m3_commands_replace_their_placeholders(command: str, option: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output
    assert option in result.output
    assert "arrives in" not in result.output
