"""M6 in the worker: the profile is read from the database at the start of every cycle. A
change applies from the next cycle with no restart (aliases and User-Agent included), cycles
wait until a profile is onboarded, an invalid stored profile keeps the last good one, and new
weights or rules re-score open matches."""

from datetime import timedelta

import pytest
from sqlalchemy import update

from jobpulse.config import FiltersConfig, ScoringConfig
from jobpulse.db import profile as profile_db
from jobpulse.db.tables import PostingRow, ProfileRow
from jobpulse.poller import NOT_ONBOARDED
from jobpulse.runtime import render_report
from tests.factories import NOW
from tests.integration.conftest import Env, GreenhouseMock, gh_job

pytestmark = pytest.mark.respx(assert_all_called=False)

OLD = NOW - timedelta(days=3)
FRESH = NOW - timedelta(minutes=25)


async def baseline(env: Env, gh: GreenhouseMock) -> None:
    gh.respond(gh_job(101, published=OLD))
    await env.poll(sources=["greenhouse"])


def clear_profile(env: Env) -> None:
    with env.sessions() as session:
        session.query(ProfileRow).delete()
        session.commit()


async def test_without_a_profile_the_cycle_waits(env: Env, gh: GreenhouseMock) -> None:
    clear_profile(env)
    report = await env.poll(sources=["greenhouse"])
    assert report.idle == NOT_ONBOARDED
    assert report.targets == []
    assert gh.route.call_count == 0
    assert render_report(report) == [f"idle: {NOT_ONBOARDED}"]


async def test_a_profile_saved_but_not_onboarded_waits_too(env: Env, gh: GreenhouseMock) -> None:
    with env.sessions() as session:
        session.execute(update(ProfileRow).values(onboarded_at=None))
        session.commit()
    report = await env.poll(sources=["greenhouse"])
    assert report.idle == NOT_ONBOARDED


async def test_one_poller_picks_up_a_profile_edit(env: Env, gh: GreenhouseMock) -> None:
    poller = env.poller()
    gh.respond(gh_job(101, published=OLD))
    await poller.run_cycle(sources=["greenhouse"])

    # A Settings save: Platform titles no longer pass, and "Bay" means San Francisco.
    filters = env.config.filters.model_dump()
    filters["title"]["include"] = [{"pattern": r"\bbackend\b", "weight": 1.0}]
    filters["locations"]["aliases"] = {"Bay": "San Francisco, CA"}
    env.reconfigure(filters=FiltersConfig.model_validate(filters))
    gh.respond(
        gh_job(101, published=OLD),
        gh_job(102, "Platform Engineer", published=FRESH),
        gh_job(103, "Backend Engineer", published=FRESH, location="Bay"),
    )
    report = await poller.run_cycle(sources=["greenhouse"])

    [target] = report.targets
    assert target.filtered == 1  # Platform Engineer, under the new rules
    assert len(target.instant) == 1
    assert env.posting("greenhouse", "103").locations == ["San Francisco, CA"]  # the new alias
    assert env.aliases["bay"] == "San Francisco, CA"


async def test_the_contact_email_reaches_the_user_agent(env: Env, gh: GreenhouseMock) -> None:
    env.reconfigure(contact_email="someone@example.org")
    await baseline(env, gh)
    assert "contact: someone@example.org" in gh.route.calls.last.request.headers["User-Agent"]


async def test_an_invalid_stored_profile_keeps_the_last_good_one(
    env: Env, gh: GreenhouseMock
) -> None:
    poller = env.poller()
    gh.respond(gh_job(101, published=OLD))
    await poller.run_cycle(sources=["greenhouse"])
    with env.sessions() as session:  # say, written by a newer version, then downgraded
        session.execute(
            update(ProfileRow).values(
                data={"timezone": "Mars/Base"}, revision=ProfileRow.revision + 1
            )
        )
        session.commit()
    gh.respond(gh_job(101, published=OLD), gh_job(102, published=FRESH))
    report = await poller.run_cycle(sources=["greenhouse"])
    assert report.profile_problem is not None
    assert "timezone" in report.profile_problem
    assert report.idle is None
    assert len(report.targets[0].instant) == 1  # still judged by the last good profile
    assert any(
        line.startswith("profile: the stored profile is invalid") for line in render_report(report)
    )

    # A fresh worker has no last good profile, so it waits instead.
    fresh = await env.poller().run_cycle(sources=["greenhouse"])
    assert fresh.idle is not None
    assert "timezone" in fresh.idle


async def test_new_weights_rescore_open_matches_once(env: Env, gh: GreenhouseMock) -> None:
    await baseline(env, gh)
    gh.respond(gh_job(101, published=OLD), gh_job(102, published=FRESH))
    await env.poll(sources=["greenhouse"])
    before = env.posting("greenhouse", "102")
    assert before.score_breakdown is not None
    assert before.score_breakdown["title"] == pytest.approx(0.30)

    scoring = env.config.scoring.model_dump()
    scoring["weights"]["title"] = 0.6
    env.reconfigure(scoring=ScoringConfig.model_validate(scoring))
    report = await env.poll(sources=["greenhouse"])

    assert report.rescored == 1
    after = env.posting("greenhouse", "102")
    assert after.score_breakdown is not None
    assert after.score_breakdown["title"] == pytest.approx(0.60)
    assert after.score == pytest.approx((before.score or 0) + 0.30)
    assert after.score_breakdown["freshness"] == before.score_breakdown["freshness"]
    assert after.tier == "instant"
    assert "profile: re-scored 1 open match (filters or scoring changed)" in render_report(report)
    assert (await env.poll(sources=["greenhouse"])).rescored == 0  # once per change


async def test_only_open_untriaged_matches_are_rescored(env: Env, gh: GreenhouseMock) -> None:
    await baseline(env, gh)
    gh.respond(
        gh_job(101, published=OLD), gh_job(102, published=FRESH), gh_job(103, published=FRESH)
    )
    await env.poll(sources=["greenhouse"])
    env.update_posting("greenhouse", "103", status="Applied")
    applied = env.posting("greenhouse", "103").score

    scoring = env.config.scoring.model_dump()
    scoring["weights"]["title"] = 0.6
    env.reconfigure(scoring=ScoringConfig.model_validate(scoring))
    report = await env.poll(sources=["greenhouse"])
    assert report.rescored == 1
    assert env.posting("greenhouse", "103").score == applied


async def test_a_new_stack_keyword_rescores_and_the_llm_part_is_kept(
    env: Env, gh: GreenhouseMock
) -> None:
    await baseline(env, gh)
    gh.descriptions[102] = "Build Go services."
    gh.respond(gh_job(101, published=OLD), gh_job(102, published=FRESH))
    await env.poll(sources=["greenhouse"])
    row = env.posting("greenhouse", "102")
    assert row.score_breakdown is not None
    assert row.score_breakdown["stack"] == 0.0
    breakdown = {**row.score_breakdown, "llm": 0.1, "total": round((row.score or 0) + 0.1, 4)}
    env.update_posting("greenhouse", "102", score_breakdown=breakdown, score=breakdown["total"])

    filters = env.config.filters.model_dump()
    filters["description"]["stack_keywords"] = {r"\bgo\b": 1.0}
    env.reconfigure(filters=FiltersConfig.model_validate(filters))
    await env.poll(sources=["greenhouse"])

    after = env.posting("greenhouse", "102")
    assert after.score_breakdown is not None
    assert after.score_breakdown["stack"] == pytest.approx(0.125)  # 0.25 times 1 / 2
    assert after.score_breakdown["llm"] == 0.1
    assert after.score == pytest.approx((row.score or 0) + 0.125 + 0.1)


async def test_a_profile_seen_for_the_first_time_rescores_nothing(
    env: Env, gh: GreenhouseMock
) -> None:
    """An imported profile is what open matches were scored with."""
    await baseline(env, gh)
    gh.respond(gh_job(101, published=OLD), gh_job(102, published=FRESH))
    await env.poll(sources=["greenhouse"])
    with env.sessions() as session:
        session.execute(update(ProfileRow).values(scored_hash=None))
        session.execute(update(PostingRow).values(score=0.01))
        session.commit()
    report = await env.poll(sources=["greenhouse"])
    assert report.rescored == 0
    assert env.posting("greenhouse", "102").score == 0.01
    with env.sessions() as session:
        assert profile_db.scored_hash(session) is not None
