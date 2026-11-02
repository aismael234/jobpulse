"""M5b: the resume store and re-scoring helpers that `jobpulse resume` and the poller share."""

from array import array
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpulse.core.filters import FilterRules
from jobpulse.core.scoring import Weights
from jobpulse.core.similarity import DEFAULT_ANCHORS, resume_lines
from jobpulse.db import resume as resume_db
from jobpulse.db.tables import PostingRow, PostingVector
from jobpulse.resume_scoring import backfill, input_hash, refresh_resume, rescore, restore_stack
from tests.factories import NOW, FakeEmbedder, posting_row, weighted

RESUME = """• Built Python services on Postgres for a payments team
• Led a React rewrite of the billing dashboard
"""
RULES = FilterRules(stack=weighted((r"\bpython\b", 1.0)), stack_saturation=1.0)
WEIGHTS = Weights()
BREAKDOWN = {"freshness": 0.2, "title": 0.3, "stack": 0.25, "level_mismatch": 0.0, "total": 0.75}


@pytest.fixture
def session(database: sessionmaker[Session]) -> Any:
    with database() as session:
        yield session


def add(session: Session, description: str | None = "Python services.", **fields: Any) -> int:
    values: dict[str, Any] = {
        "description_text": description,
        "score": 0.75,
        "score_breakdown": dict(BREAKDOWN),
        **fields,
    }
    row = posting_row(**values)
    session.add(row)
    session.flush()
    return row.id


def store_resume(session: Session, embedder: FakeEmbedder, text: str = RESUME) -> None:
    resume_db.put(
        session,
        filename="resume.txt",
        media_type="text/plain",
        text=text,
        lines=resume_lines(text),
        now=NOW,
    )
    assert refresh_resume(session, embedder)


def test_vectors_round_trip_through_bytes() -> None:
    vectors = [array("f", [0.5, -0.25, 1.0]), array("f", [0.0, 0.125, -1.0])]
    blob = resume_db.encode(vectors)
    assert len(blob) == 2 * 3 * 4
    assert resume_db.decode(blob, dims=3) == vectors


def test_put_replaces_the_resume_and_clears_its_vectors(session: Session) -> None:
    embedder = FakeEmbedder()
    store_resume(session, embedder)
    stored = resume_db.get(session)
    assert stored is not None
    assert stored.embed_model == embedder.model
    assert stored.vectors is not None
    assert len(stored.vectors.lines) == 2

    resume_db.put(
        session, filename="new.md", media_type="text/markdown", text="x", lines=[], now=NOW
    )
    replaced = resume_db.get(session)
    assert replaced is not None
    assert (replaced.filename, replaced.embed_model, replaced.vectors) == ("new.md", None, None)
    assert replaced.anchors == DEFAULT_ANCHORS
    assert resume_db.clear(session)
    assert resume_db.get(session) is None
    assert not resume_db.clear(session)


def test_refresh_resume_embeds_only_when_the_model_differs(session: Session) -> None:
    embedder = FakeEmbedder()
    store_resume(session, embedder)
    calls = embedder.calls
    assert not refresh_resume(session, embedder)
    assert embedder.calls == calls


def test_backfill_embeds_open_new_and_saved_matches_missing_a_vector(session: Session) -> None:
    embedder = FakeEmbedder()
    store_resume(session, embedder)
    wanted = add(session, "Python services.")
    saved = add(session, "Go services.", status="Saved")
    add(session, "Applied already.", status="Applied")
    add(session, "Closed.", closed_at=NOW - timedelta(hours=1))
    add(session, "Filtered.", tier="filtered")
    add(session, None)
    cached = add(session, "Already embedded.")
    resume_db.save_posting_vectors(
        session,
        {cached: (input_hash(embedder.model, "Already embedded."), embedder.embed(["x"]))},
        model=embedder.model,
        now=NOW,
    )
    progress: list[int] = []
    embedded_before = list(embedder.texts)

    assert backfill(session, embedder, now=NOW, progress=progress.append) == 2

    assert set(embedder.texts) - set(embedded_before) == {"Python services.", "Go services."}
    rows = session.scalars(select(PostingVector.posting_id)).all()
    assert sorted(rows) == sorted([wanted, saved, cached])
    assert progress[-1] == 2


def test_rescore_measures_anchors_once_there_are_enough_matches(session: Session) -> None:
    embedder = FakeEmbedder()
    store_resume(session, embedder)
    ids = [add(session, text) for text in ("Python services.", "React billing.", "Go deploys.")]
    backfill(session, embedder, now=NOW)

    report = rescore(session, rules=RULES, weights=WEIGHTS, now=NOW, min_samples=3)

    assert report.rescored == 3
    assert report.measured_on == 3
    stored = resume_db.get(session)
    assert stored is not None
    assert stored.anchors != DEFAULT_ANCHORS
    assert stored.anchor_count == 3
    assert stored.rescored_at == NOW
    scores = [session.get(PostingRow, i).resume_score for i in ids]  # type: ignore[union-attr]
    assert min(s for s in scores if s is not None) == 0.0  # p5 of three is near the lowest
    assert max(s for s in scores if s is not None) == pytest.approx(1.0, abs=0.1)


def test_rescore_with_too_few_matches_keeps_the_default_anchors(session: Session) -> None:
    embedder = FakeEmbedder()
    store_resume(session, embedder)
    add(session, "Python services.")
    backfill(session, embedder, now=NOW)

    report = rescore(session, rules=RULES, weights=WEIGHTS, now=NOW)

    assert (report.rescored, report.measured_on) == (1, 0)
    stored = resume_db.get(session)
    assert stored is not None
    assert (stored.anchors, stored.anchor_count) == (DEFAULT_ANCHORS, 0)


def test_restore_stack_puts_the_stack_part_back(session: Session) -> None:
    resumed = {**BREAKDOWN, "stack": None, "resume": 0.1, "llm": 0.05, "total": 0.65}
    with_resume = add(
        session,
        "Python services.",
        score=0.65,
        score_breakdown=resumed,
        resume_score=0.4,
        resume_line="Built Python services",
    )
    applied = add(
        session,
        "Python services.",
        status="Applied",
        score=0.65,
        score_breakdown=dict(resumed),
        resume_score=0.4,
        resume_line="Built Python services",
    )

    assert restore_stack(session, rules=RULES, weights=WEIGHTS) == 1

    row = session.get(PostingRow, with_resume)
    assert row is not None
    session.refresh(row)
    assert row.score_breakdown is not None
    assert row.score_breakdown["stack"] == pytest.approx(0.25)
    assert "resume" not in row.score_breakdown
    assert row.score_breakdown["llm"] == 0.05
    assert row.score == pytest.approx(0.2 + 0.3 + 0.25 + 0.05)
    assert (row.resume_score, row.resume_line) == (None, None)
    kept = session.get(PostingRow, applied)
    assert kept is not None
    assert kept.resume_line == "Built Python services"  # triaged postings keep their score
