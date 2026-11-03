"""M5b: resume similarity through the whole cycle, over a fake embedder. The resume part replaces
the stack part only for matches with a description while a resume is stored; descriptions are
embedded once and cached by input hash; a changed resume (or model) re-scores open New and
Saved matches at the next cycle start, keeping the LLM's part; an embedder failure falls back
to the stack keywords; a dry run persists nothing."""

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from jobpulse.core.similarity import (
    DEFAULT_ANCHORS,
    ResumeVectors,
    calibrate,
    resume_chunks,
    resume_lines,
    resume_raw,
)
from jobpulse.db.tables import PostingVector, ResumeRow
from jobpulse.embeddings.base import EmbedderUnavailable
from jobpulse.runtime import render_report
from tests.factories import NOW, FakeEmbedder
from tests.integration.conftest import DEFAULT_DESCRIPTION, Env, GreenhouseMock, gh_job

RESUME = """Experience
• Built Python services on Postgres for a payments team
• Led a React rewrite of the billing dashboard
• Wrote Go tooling for deploys and on-call
"""
OTHER_RESUME = """Projects
• Trained vision models for warehouse robots
• Shipped an iOS app for field technicians
"""
FRESH = NOW - timedelta(minutes=25)
OLD = gh_job(4012345200, "Backend Engineer, Ledger", published=NOW - timedelta(days=40))
PAYMENTS = gh_job(4012345201, "Backend Engineer, Payments", published=FRESH)
SEARCH = gh_job(4012345202, "Backend Engineer, Search", published=FRESH)
BILLING = gh_job(4012345203, "Backend Engineer, Billing", published=FRESH)
FILTERED = gh_job(4012345204, "Engineering Manager, Backend", published=FRESH)


class FailingEmbedder(FakeEmbedder):
    def embed(self, texts: Sequence[str]) -> list[Any]:
        raise EmbedderUnavailable("model not downloaded")


@pytest.fixture
def embedder(env: Env) -> FakeEmbedder:
    env.embedder = FakeEmbedder()
    return env.embedder


async def after_baseline(env: Env, gh: GreenhouseMock, *jobs: dict[str, Any], **kw: Any) -> Any:
    gh.respond(OLD)
    await env.poll(sources=["greenhouse"])
    gh.respond(OLD, *jobs)
    return await env.poll(sources=["greenhouse"], **kw)


def vectors(env: Env) -> list[PostingVector]:
    with env.sessions() as session:
        return list(session.scalars(select(PostingVector).order_by(PostingVector.posting_id)))


def expected_score(text: str, resume: str) -> float:
    fake = FakeEmbedder()
    lines = resume_lines(resume)
    stored = ResumeVectors(
        lines=tuple(fake.embed(lines)), chunks=tuple(fake.embed(resume_chunks(lines)))
    )
    found = resume_raw(fake.embed([text]), stored)
    assert found is not None
    return calibrate(found.raw, DEFAULT_ANCHORS)


async def test_the_resume_part_replaces_the_stack_part_for_a_match_with_a_description(
    env: Env, gh: GreenhouseMock, embedder: FakeEmbedder
) -> None:
    env.set_resume(RESUME)
    report = await after_baseline(env, gh, PAYMENTS, FILTERED)

    row = env.posting("greenhouse", "4012345201")
    assert row.tier == "instant"
    assert row.score_breakdown is not None
    assert row.score_breakdown["stack"] is None
    assert row.resume_score == pytest.approx(expected_score(DEFAULT_DESCRIPTION, RESUME), abs=1e-4)
    assert row.score_breakdown["resume"] == pytest.approx(0.25 * row.resume_score, abs=1e-4)
    assert row.score == pytest.approx(row.score_breakdown["total"])
    assert row.resume_line == "Built Python services on Postgres for a payments team"
    assert env.posting("greenhouse", "4012345204").resume_score is None  # filtered: not embedded
    assert [v.posting_id for v in vectors(env)] == [row.id]
    assert DEFAULT_DESCRIPTION in embedder.texts
    assert report.resume_matched == 1
    assert report.resume_embedded == 1
    assert any(line.startswith("resume: 1 match scored") for line in render_report(report))


async def test_without_a_resume_the_stack_part_stays_and_nothing_is_embedded(
    env: Env, gh: GreenhouseMock, embedder: FakeEmbedder
) -> None:
    await after_baseline(env, gh, PAYMENTS)

    row = env.posting("greenhouse", "4012345201")
    assert row.score_breakdown is not None
    assert row.score_breakdown["stack"] == pytest.approx(0.125)  # python, saturation 2
    assert "resume" not in row.score_breakdown
    assert row.resume_score is None
    assert embedder.calls == 0
    assert vectors(env) == []


async def test_a_match_without_a_description_keeps_the_stack_rules(
    env: Env, gh: GreenhouseMock, embedder: FakeEmbedder
) -> None:
    env.set_resume(RESUME)
    gh.descriptions = {4012345201: ""}
    await after_baseline(env, gh, PAYMENTS)

    row = env.posting("greenhouse", "4012345201")
    assert row.tier == "instant"
    assert row.score_breakdown is not None
    assert "resume" not in row.score_breakdown
    assert row.resume_score is None
    assert vectors(env) == []


async def test_identical_descriptions_are_embedded_once(
    env: Env, gh: GreenhouseMock, embedder: FakeEmbedder
) -> None:
    env.set_resume(RESUME)
    report = await after_baseline(env, gh, PAYMENTS, SEARCH)

    assert embedder.texts.count(DEFAULT_DESCRIPTION) == 1
    assert len(vectors(env)) == 2
    assert report.resume_matched == 2
    assert report.resume_embedded == 1


async def test_an_embedder_failure_falls_back_to_the_stack_part(
    env: Env, gh: GreenhouseMock
) -> None:
    env.embedder = FailingEmbedder()
    env.set_resume(RESUME)
    report = await after_baseline(env, gh, PAYMENTS)

    row = env.posting("greenhouse", "4012345201")
    assert row.tier == "instant"
    assert row.score_breakdown is not None
    assert row.score_breakdown["stack"] == pytest.approx(0.125)  # python, saturation 2
    assert row.resume_score is None
    assert report.resume_failed is not None
    assert "model not downloaded" in report.resume_failed
    assert any(
        line.startswith("resume:") and "stack keywords" in line for line in render_report(report)
    )


async def test_a_dry_run_persists_nothing(
    env: Env, gh: GreenhouseMock, embedder: FakeEmbedder
) -> None:
    gh.respond(OLD)
    await env.poll(sources=["greenhouse"])
    env.set_resume(RESUME)
    gh.respond(OLD, PAYMENTS)
    report = await env.poll(sources=["greenhouse"], dry_run=True)

    assert report.resume_matched == 1
    assert vectors(env) == []
    stored = env.resume()
    assert stored is not None
    assert stored.embed_model is None  # the cycle embedded it, then rolled back
    assert stored.rescored_at is None


async def test_a_new_resume_rescores_open_new_and_saved_matches_only(
    env: Env, gh: GreenhouseMock, embedder: FakeEmbedder
) -> None:
    env.set_resume(RESUME)
    gh.descriptions = {
        4012345201: "Python services for payments on Postgres.",
        4012345202: "Vision models for robots, Python.",
        4012345203: "iOS app for billing, Python.",
    }
    await after_baseline(env, gh, PAYMENTS, SEARCH, BILLING)
    applied = env.posting("greenhouse", "4012345203")
    env.update_posting("greenhouse", "4012345202", status="Saved")
    env.update_posting("greenhouse", "4012345203", status="Applied")
    new = env.posting("greenhouse", "4012345201")
    assert new.score_breakdown is not None
    env.update_posting(
        "greenhouse",
        "4012345201",
        score_breakdown={**new.score_breakdown, "llm": 0.1},
    )
    embedded_before = len(embedder.texts)

    env.set_resume(OTHER_RESUME)
    report = await env.poll(sources=["greenhouse"])

    assert report.resume_rescored == 2
    rescored_new = env.posting("greenhouse", "4012345201")
    saved = env.posting("greenhouse", "4012345202")
    assert saved.resume_line == "Trained vision models for warehouse robots"
    assert rescored_new.resume_line in resume_lines(OTHER_RESUME)
    assert rescored_new.score_breakdown is not None
    assert rescored_new.score_breakdown["llm"] == 0.1  # the LLM's part is kept
    assert rescored_new.score == pytest.approx(rescored_new.score_breakdown["total"])
    assert rescored_new.resume_score == pytest.approx(
        expected_score("Python services for payments on Postgres.", OTHER_RESUME), abs=1e-4
    )
    unchanged = env.posting("greenhouse", "4012345203")
    assert (unchanged.resume_line, unchanged.score) == (applied.resume_line, applied.score)
    # The postings' vectors came from the cache: only the new resume was embedded.
    assert set(embedder.texts[embedded_before:]) <= set(
        resume_lines(OTHER_RESUME) + resume_chunks(resume_lines(OTHER_RESUME))
    )
    stored = env.resume()
    assert stored is not None
    assert stored.rescored_at is not None
    assert stored.embed_model == embedder.model

    again = await env.poll(sources=["greenhouse"])  # nothing changed: no second re-score
    assert again.resume_rescored == 0


async def test_a_changed_model_re_embeds_the_resume_and_rescores(
    env: Env, gh: GreenhouseMock, embedder: FakeEmbedder
) -> None:
    env.set_resume(RESUME)
    await after_baseline(env, gh, PAYMENTS)
    with env.sessions() as session:
        resume_db_row = session.get(ResumeRow, 1)
        assert resume_db_row is not None
        resume_db_row.embed_model = "older-model@0"
        session.commit()
    embedded_before = len(embedder.texts)

    report = await env.poll(sources=["greenhouse"])

    assert set(resume_lines(RESUME)) <= set(embedder.texts[embedded_before:])
    stored = env.resume()
    assert stored is not None
    assert stored.embed_model == embedder.model
    assert report.resume_rescored == 1


async def test_a_resume_without_content_lines_is_never_matched(
    env: Env, gh: GreenhouseMock, embedder: FakeEmbedder
) -> None:
    env.set_resume("Jane Doe\njane@example.com\n")
    await after_baseline(env, gh, PAYMENTS)

    row = env.posting("greenhouse", "4012345201")
    assert row.resume_score is None
    assert row.score_breakdown is not None
    assert row.score_breakdown["stack"] == pytest.approx(0.125)  # python, saturation 2
