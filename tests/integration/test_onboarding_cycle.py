"""M8 in the poller: a profile built from setup answers drives a cycle (the first one a silent
baseline), dream companies get the top company priority, the pay floor filters, and the seed
applied after onboarding searches Workday boards with keywords from the chosen roles."""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import delete

from jobpulse.config import Profile, SetupAnswers, compose, split
from jobpulse.core.atsurls import AtsRef
from jobpulse.db.tables import ProfileRow
from jobpulse.onboarding import apply_answers, apply_seed
from jobpulse.seed import Seed, SeedBoard
from jobpulse.sources.ats_detect import Validation
from tests.factories import NOW, jobspy_frame, jobspy_row
from tests.integration.conftest import Env, GreenhouseMock, ScriptedScrape, gh_job

pytestmark = pytest.mark.respx(assert_all_called=False)


async def test_the_first_cycle_after_onboarding_is_a_silent_baseline(
    env: Env, gh: GreenhouseMock
) -> None:
    with env.sessions() as session:
        session.execute(delete(ProfileRow))
        session.commit()
    idle = await env.poll(sources=["greenhouse"])
    assert idle.idle is not None

    infra, _ = split(env.config)
    answers = SetupAnswers(roles=["backend"], channel="none")
    env.config = compose(infra, apply_answers(Profile(), answers))
    gh.respond(gh_job(101, published=NOW - timedelta(minutes=5)))
    report = await env.poll(sources=["greenhouse"])

    assert report.idle is None
    assert [target.is_baseline for target in report.targets] == [True]
    assert env.notifier.sent == []
    assert [row.tier for row in env.postings()] == ["baseline"]


async def test_a_dream_company_gets_the_top_company_priority(
    env: Env, boards: ScriptedScrape
) -> None:
    env.reconfigure(scoring={"dream_companies": ["Northwind"], "instant_threshold": 0.5})
    env.add_search()
    boards.queue(
        "linkedin",
        jobspy_frame([]),
        jobspy_frame(
            [
                jobspy_row("linkedin", external_id="4101000061", company="Northwind, Inc."),
                jobspy_row(
                    "linkedin",
                    external_id="4101000062",
                    company="Southwind",
                    title="Platform Engineer",
                ),
            ]
        ),
    )
    poller = env.poller()
    await poller.run_cycle(sources=["linkedin"])
    await poller.run_cycle(sources=["linkedin"])
    dream = env.posting("linkedin", "4101000061").score_breakdown
    other = env.posting("linkedin", "4101000062").score_breakdown
    assert dream is not None
    assert other is not None
    assert dream["company_priority"] == pytest.approx(env.config.scoring.weights.company_priority)
    assert other["company_priority"] == 0.0


async def test_the_pay_floor_filters_postings_paying_less(env: Env, boards: ScriptedScrape) -> None:
    env.reconfigure(filters={"pay": {"min_yearly_usd": 200_000}})
    env.add_search("backend-indeed", site="indeed", search_term="backend engineer")
    boards.queue(
        "indeed",
        jobspy_frame([]),
        jobspy_frame(
            [
                jobspy_row("indeed", external_id="aaa111"),  # $150K–$190K
                jobspy_row(
                    "indeed",
                    external_id="bbb222",
                    title="Platform Engineer",
                    min_amount=210_000.0,
                    max_amount=240_000.0,
                ),
            ]
        ),
    )
    poller = env.poller()
    await poller.run_cycle(sources=["indeed"])
    await poller.run_cycle(sources=["indeed"])
    low = env.posting("indeed", "aaa111")
    assert (low.tier, low.tier_reason) == ("filtered", "pays at most $190K a year (floor $200K)")
    assert env.posting("indeed", "bbb222").tier == "instant"


class FakeDetector:
    """Records the Workday keywords searched; each finds a few matching jobs."""

    def __init__(self) -> None:
        self.keywords: list[str] = []

    async def workday_search(self, ref: AtsRef, keyword: str) -> Validation:
        self.keywords.append(keyword)
        return Validation(ref, ok=True, job_count=3, titles=("Frontend Engineer",))


async def test_the_seed_searches_workday_with_keywords_from_the_roles(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = Seed(
        (
            SeedBoard("greenhouse", "northwind", "Northwind"),
            SeedBoard("workday", "acme.wd5/External", "Acme Corp"),
        )
    )
    monkeypatch.setattr("jobpulse.onboarding.packaged_seed", lambda: seed)
    infra, _ = split(env.config)
    config = compose(infra, apply_answers(Profile(), SetupAnswers(roles=["frontend", "mobile"])))
    detector: Any = FakeDetector()

    added = await apply_seed(env.sessions, config, detector, now=NOW)

    assert detector.keywords == ["frontend engineer", "mobile engineer"]
    assert added == ["greenhouse/northwind", "workday/acme.wd5/External"]
    workday = env.company("acme.wd5/External")
    assert workday.source_of_discovery == "seed"
    assert workday.baseline_done_at is None
    assert workday.params == {"keywords": ["frontend engineer", "mobile engineer"]}
