"""M5b: `jobpulse resume set | show | clear | rescore`."""

from pathlib import Path
from typing import Any

import pytest
import time_machine
from sqlalchemy import select
from typer.testing import CliRunner

from jobpulse import cli
from jobpulse.db.session import create_engine_for, make_session_factory
from jobpulse.db.tables import PostingRow, PostingVector
from tests.factories import NOW, FakeEmbedder, posting_row

runner = CliRunner()
RESUME = """Jane Doe
jane@example.com · (555) 123-4567
Experience
• Built Python services on Postgres for a payments team
• Led a React rewrite of the billing dashboard
"""
# Preferences as an old config.yaml had them, brought in with `profile import`.
PROFILE = """contact_email: dev@example.com
filters:
  description:
    stack_keywords:
      '\\bpython\\b': 1.0
"""


@pytest.fixture
def base(
    tmp_path: Path, time_machine: time_machine.TimeMachineFixture, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    time_machine.move_to(NOW, tick=False)
    db_url = f"sqlite:///{tmp_path / 'jobpulse.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    config = tmp_path / "config.yaml"
    config.write_text("", encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(PROFILE, encoding="utf-8")
    args = ["--config", str(config), "--env-file", str(tmp_path / ".env")]
    upgraded = runner.invoke(cli.app, [*args, "db", "upgrade"])
    assert upgraded.exit_code == 0, upgraded.output
    imported = runner.invoke(cli.app, [*args, "profile", "import", str(profile)])
    assert imported.exit_code == 0, imported.output
    return args


def sessions() -> Any:
    import os

    return make_session_factory(create_engine_for(os.environ["DATABASE_URL"]))


@pytest.fixture
def resume_file(tmp_path: Path) -> Path:
    path = tmp_path / "resume.txt"
    path.write_text(RESUME, encoding="utf-8")
    return path


@pytest.fixture
def embedder(monkeypatch: pytest.MonkeyPatch) -> FakeEmbedder:
    fake = FakeEmbedder()
    monkeypatch.setattr(cli, "build_embedder", lambda config: fake)
    return fake


def invoke(base: list[str], *args: str) -> Any:
    return runner.invoke(cli.app, [*base, "resume", *args])


def test_set_then_show_never_prints_personal_lines(base: list[str], resume_file: Path) -> None:
    result = invoke(base, "set", str(resume_file))
    assert result.exit_code == 0, result.output
    assert "2 content lines" in result.output
    assert "jobpulse resume rescore" in result.output

    shown = invoke(base, "show", "--lines")
    assert shown.exit_code == 0, shown.output
    assert "resume.txt" in shown.output
    assert "not embedded yet" in shown.output
    assert "Built Python services on Postgres for a payments team" in shown.output
    for personal in ("Jane", "jane@example.com", "555"):
        assert personal not in shown.output


def test_set_refuses_a_file_without_content_lines(base: list[str], tmp_path: Path) -> None:
    empty = tmp_path / "resume.txt"
    empty.write_text("Jane Doe\njane@example.com\n", encoding="utf-8")
    result = invoke(base, "set", str(empty))
    assert result.exit_code == 2
    assert "no content lines" in result.output


def test_show_without_a_resume(base: list[str]) -> None:
    result = invoke(base, "show")
    assert result.exit_code == 0
    assert "no resume" in result.output


def test_rescore_with_backfill_embeds_and_scores_open_matches(
    base: list[str], resume_file: Path, embedder: FakeEmbedder
) -> None:
    assert invoke(base, "set", str(resume_file)).exit_code == 0
    breakdown = {"freshness": 0.2, "title": 0.3, "stack": 0.25, "total": 0.75}
    with sessions()() as session:
        row = posting_row(
            description_text="Python services on Postgres.", score=0.75, score_breakdown=breakdown
        )
        session.add(row)
        session.commit()
        row_id = row.id

    plain = invoke(base, "rescore")
    assert plain.exit_code == 0, plain.output
    assert "1 open match has no vector" in plain.output
    assert "--backfill" in plain.output

    result = invoke(base, "rescore", "--backfill")
    assert result.exit_code == 0, result.output
    assert "embedded 1 description" in result.output
    assert "re-scored 1 open match" in result.output
    with sessions()() as session:
        stored = session.get(PostingRow, row_id)
        assert stored is not None
        assert stored.resume_line == "Built Python services on Postgres for a payments team"
        assert stored.score_breakdown is not None
        assert stored.score_breakdown["stack"] is None
        assert session.scalars(select(PostingVector.posting_id)).all() == [row_id]

    cleared = invoke(base, "clear")
    assert cleared.exit_code == 0, cleared.output
    assert "1 open match back on stack keywords" in cleared.output
    with sessions()() as session:
        restored = session.get(PostingRow, row_id)
        assert restored is not None
        assert restored.resume_score is None
        assert restored.score_breakdown is not None
        assert restored.score_breakdown["stack"] == pytest.approx(0.125)  # saturation 2


def test_rescore_without_the_model_explains_how_to_get_it(
    base: list[str], resume_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobpulse.embeddings.base import EmbedderUnavailable

    class Missing(FakeEmbedder):
        def embed(self, texts: Any) -> Any:
            raise EmbedderUnavailable("BAAI/bge-small-en-v1.5 isn't in data/models")

    monkeypatch.setattr(cli, "build_embedder", lambda config: Missing())
    assert invoke(base, "set", str(resume_file)).exit_code == 0
    result = invoke(base, "rescore")
    assert result.exit_code == 2
    assert "isn't in data/models" in result.output


def test_rescore_without_a_resume(base: list[str], embedder: FakeEmbedder) -> None:
    result = invoke(base, "rescore")
    assert result.exit_code == 2
    assert "no resume" in result.output
