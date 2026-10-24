"""M4: the LLM layer through the whole cycle, over a fake provider. Only rule survivors in the
band are asked about, an answer moves the score and the tier, answers are cached per posting,
the daily cap and failures leave postings to the rules, dry runs never call, and a bad key
stops the asking."""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from jobpulse.core.assessment import Assessment
from jobpulse.db.tables import LlmAssessment
from jobpulse.llm.base import AssessmentRequest, LlmReply
from jobpulse.llm.scorer import LlmScorer
from jobpulse.runtime import render_report
from tests.factories import NOW
from tests.integration.conftest import Env, GreenhouseMock, gh_job

RESUME = "Backend engineer: Python, Go, Postgres. 4 years."
FRESH = NOW - timedelta(minutes=25)
OLD = gh_job(4012345100, "Backend Engineer, Ledger", published=NOW - timedelta(days=40))
# Score with the test config (threshold 0.5, band 0.1): ~0.77, ~0.59 and filtered.
STRONG = gh_job(4012345101, "Backend Engineer, Payments", published=FRESH)
BAND = gh_job(4012345102, "Platform Engineer", published=FRESH)
BAND_TOO = gh_job(4012345103, "Platform Engineer, Data", published=FRESH)
FILTERED = gh_job(4012345104, "Engineering Manager, Platform", published=FRESH)
WEAK = LlmReply(model="claude-opus-5-5", assessment=Assessment("mid", 0.2, 0.2, "Mostly frontend."))


class FakeLlm:
    """The provider: answers by the posting's title (a good fit unless told otherwise)."""

    model = "claude-opus-5-5"

    def __init__(self) -> None:
        self.answers: dict[str, LlmReply] = {}
        self.requests: list[AssessmentRequest] = []

    async def assess(self, request: AssessmentRequest) -> LlmReply:
        self.requests.append(request)
        for title, reply in self.answers.items():
            if f"Title: {title}\n" in request.posting:
                return reply
        return LlmReply(model=self.model, assessment=Assessment("mid", 0.9, 0.9, "Good fit."))

    async def aclose(self) -> None:
        pass

    def titles(self) -> list[str]:
        return [
            line.removeprefix("Title: ")
            for request in self.requests
            for line in request.posting.splitlines()
            if line.startswith("Title: ")
        ]


def attach(env: Env, client: FakeLlm, **options: Any) -> None:
    values: dict[str, Any] = {
        "resume": RESUME,
        "model": client.model,
        "effort": "low",
        "band": 0.10,
        "threshold": env.config.scoring.instant_threshold,
        "weight": env.config.scoring.weights.llm,
        "max_calls_per_day": 25,
        **options,
    }
    env.llm = LlmScorer(client, **values)


@pytest.fixture
def llm(env: Env, gh: GreenhouseMock) -> FakeLlm:
    client = FakeLlm()
    attach(env, client)
    gh.descriptions = {  # no "python": these stay in the band
        4012345102: "Build Go services on Postgres.",
        4012345103: "Build Go services for the data team.",
        4012345105: "Build Go services for search.",
    }
    return client


async def after_baseline(env: Env, gh: GreenhouseMock, *jobs: dict[str, Any]) -> Any:
    gh.respond(OLD)
    await env.poll(sources=["greenhouse"])
    gh.respond(OLD, *jobs)
    return await env.poll(sources=["greenhouse"])


def assessments(env: Env) -> list[LlmAssessment]:
    with env.sessions() as session:
        return list(session.scalars(select(LlmAssessment).order_by(LlmAssessment.id)))


async def test_only_the_band_is_asked_about_and_the_answer_moves_the_tier(
    env: Env, gh: GreenhouseMock, llm: FakeLlm
) -> None:
    llm.answers["Platform Engineer"] = WEAK
    report = await after_baseline(env, gh, STRONG, BAND, FILTERED)

    assert llm.titles() == ["Platform Engineer"]  # not the strong match, not the filtered one
    [request] = llm.requests
    assert request.resume == RESUME
    assert "Build Go services on Postgres." in request.posting
    band = env.posting("greenhouse", "4012345102")
    assert band.tier == "digest"  # was instant on the rules alone
    assert band.score is not None
    assert band.score < 0.5
    assert band.score_breakdown is not None
    assert band.score_breakdown["llm"] == pytest.approx(-0.09)
    assert band.tier_reason is not None
    assert "llm -0.09" in band.tier_reason
    assert [push.title.split(" · ")[1] for push in env.notifier.sent] == [
        "Backend Engineer, Payments"
    ]
    [stored] = assessments(env)
    assert (stored.posting_id, stored.ok, stored.level, stored.model) == (
        band.id,
        True,
        "mid",
        "claude-opus-5-5",
    )
    assert stored.input_hash == request.input_hash
    assert report.llm_asked == 1
    assert any(line.startswith("llm: 1 answered") for line in render_report(report))


async def test_an_answer_is_cached_per_posting(env: Env, gh: GreenhouseMock, llm: FakeLlm) -> None:
    llm.answers["Platform Engineer"] = WEAK
    await after_baseline(env, gh, BAND)
    env.update_posting("greenhouse", "4012345102", tier="pending")  # as if a cycle broke off

    report = await env.poll(sources=["greenhouse"])

    assert len(llm.requests) == 1  # the second decision used the stored answer
    assert report.llm_cached == 1
    assert env.posting("greenhouse", "4012345102").tier == "digest"
    assert len(assessments(env)) == 1


async def test_a_changed_resume_asks_again(env: Env, gh: GreenhouseMock, llm: FakeLlm) -> None:
    await after_baseline(env, gh, BAND)
    env.update_posting("greenhouse", "4012345102", tier="pending")
    attach(env, llm, resume=RESUME + " Now 5 years.")
    await env.poll(sources=["greenhouse"])
    assert len(llm.requests) == 2


async def test_the_daily_cap_leaves_the_rest_to_the_rules(
    env: Env, gh: GreenhouseMock, llm: FakeLlm
) -> None:
    attach(env, llm, max_calls_per_day=1)
    report = await after_baseline(env, gh, BAND, BAND_TOO)

    assert len(llm.requests) == 1
    assert report.llm_over_limit == 1
    assert any("llm.max_calls_per_day" in line for line in render_report(report))
    asked, left = (env.posting("greenhouse", job_id) for job_id in ("4012345102", "4012345103"))
    answered = [
        row for row in (asked, left) if row.score_breakdown and "llm" in row.score_breakdown
    ]
    assert len(answered) == 1
    assert {row.tier for row in (asked, left)} == {"instant"}  # a good fit, and the rules

    gh.respond(
        OLD, BAND, BAND_TOO, gh_job(4012345105, "Platform Engineer, Search", published=FRESH)
    )
    again = await env.poll(sources=["greenhouse"])
    assert len(llm.requests) == 1  # still inside the same 24 hours
    assert again.llm_over_limit == 1


async def test_a_failed_call_counts_and_leaves_the_posting_to_the_rules(
    env: Env, gh: GreenhouseMock, llm: FakeLlm
) -> None:
    llm.answers["Platform Engineer"] = LlmReply(
        model="claude-opus-5-5", error="HTTP 529: overloaded"
    )
    report = await after_baseline(env, gh, BAND)

    assert report.llm_failed == 1
    band = env.posting("greenhouse", "4012345102")
    assert band.tier == "instant"
    assert band.score_breakdown is not None
    assert "llm" not in band.score_breakdown
    [stored] = assessments(env)
    assert (stored.ok, stored.error) == (False, "HTTP 529: overloaded")
    assert len(env.notifier.sent) == 1


async def test_a_dry_run_never_calls_but_uses_cached_answers(
    env: Env, gh: GreenhouseMock, llm: FakeLlm
) -> None:
    gh.respond(OLD)
    await env.poll(sources=["greenhouse"])
    gh.respond(OLD, BAND)
    dry = await env.poll(sources=["greenhouse"], dry_run=True)
    assert llm.requests == []
    assert dry.llm_would_ask == 1
    assert any("would ask" in line for line in render_report(dry))
    assert assessments(env) == []

    llm.answers["Platform Engineer"] = WEAK
    await env.poll(sources=["greenhouse"])  # the live run asks
    env.update_posting("greenhouse", "4012345102", tier="pending")
    cached = await env.poll(sources=["greenhouse"], dry_run=True)
    assert len(llm.requests) == 1
    assert cached.llm_cached == 1
    assert cached.targets[0].digest  # the cached answer still moved it to the digest


async def test_a_bad_key_stops_the_asking(env: Env, gh: GreenhouseMock, llm: FakeLlm) -> None:
    llm.answers["Platform Engineer"] = LlmReply(
        model="claude-opus-5-5", error="HTTP 401: invalid x-api-key", fatal=True
    )
    report = await after_baseline(env, gh, BAND)
    assert report.llm_stopped is not None
    assert "401" in report.llm_stopped
    assert any("stopped" in line for line in render_report(report))

    gh.respond(OLD, BAND, BAND_TOO)
    await env.poll(sources=["greenhouse"])
    assert len(llm.requests) == 1


async def test_without_the_layer_nothing_is_asked(env: Env, gh: GreenhouseMock) -> None:
    report = await after_baseline(env, gh, BAND)
    assert env.llm is None
    assert (report.llm_asked, report.llm_would_ask) == (0, 0)
    assert env.posting("greenhouse", "4012345102").tier == "instant"
    assert not any(line.startswith("llm:") for line in render_report(report))
