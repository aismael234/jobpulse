"""M5.0 through the cycle: a new posting whose Greenhouse requisition was first seen at least
7 days earlier (open or closed) is stored as a repost; the same company, title and place alone
is not one. Every decided posting gets its employment type. Neither changes what is matched
or pushed."""

from datetime import timedelta
from typing import Any

import pytest

from tests.factories import NOW
from tests.integration.conftest import Env, GreenhouseMock, board_posting, gh_job

pytestmark = pytest.mark.respx(assert_all_called=False)

OLD = NOW - timedelta(days=30)
FRESH = NOW - timedelta(minutes=25)


async def baseline_then_age(
    env: Env, gh: GreenhouseMock, job: dict[str, Any], age: timedelta
) -> None:
    """Job 101 stored in the baseline, then made to look first seen ``age`` ago and closed."""
    gh.respond(job)
    await env.poll(sources=["greenhouse"])
    env.update_posting(
        "greenhouse", str(job["id"]), first_seen_at=NOW - age, closed_at=NOW - timedelta(days=1)
    )


async def test_a_requisition_seen_a_week_earlier_is_a_repost_and_still_pushed(
    env: Env, gh: GreenhouseMock
) -> None:
    await baseline_then_age(env, gh, gh_job(101, published=OLD), timedelta(days=10))
    new = gh_job(201, "Backend Engineer II", published=FRESH, location="Austin, TX")
    new["internal_job_id"] = 900_101  # job 101's requisition
    gh.respond(new)

    await env.poll(sources=["greenhouse"])

    row = env.posting("greenhouse", "201")
    assert row.repost_reason == "first seen 10 days ago (requisition 900101)"
    assert row.tier == "instant"
    assert [line.split(" · ")[1] for line in env.pushed] == ["Backend Engineer II"]


async def test_siblings_posted_within_a_week_are_not_reposts(env: Env, gh: GreenhouseMock) -> None:
    await baseline_then_age(env, gh, gh_job(101, published=OLD), timedelta(days=6))
    new = gh_job(201, "Backend Engineer II", published=FRESH, location="Austin, TX")
    new["internal_job_id"] = 900_101
    gh.respond(new)

    await env.poll(sources=["greenhouse"])

    assert env.posting("greenhouse", "201").repost_reason is None


async def test_another_opening_with_the_same_title_and_place_is_not_a_repost(
    env: Env, gh: GreenhouseMock
) -> None:
    """Companies hire several people under one generic title: a new requisition is a new job."""
    await baseline_then_age(env, gh, gh_job(101, published=OLD), timedelta(days=9))
    gh.respond(gh_job(202, published=FRESH))  # same title and place, a new requisition

    await env.poll(sources=["greenhouse"])

    row = env.posting("greenhouse", "202")
    assert row.repost_reason is None
    assert row.tier == "instant"


async def test_a_new_posting_is_not_a_repost(env: Env, gh: GreenhouseMock) -> None:
    await baseline_then_age(env, gh, gh_job(101, published=OLD), timedelta(days=9))
    gh.respond(gh_job(203, "Platform Engineer", published=FRESH))

    await env.poll(sources=["greenhouse"])

    assert env.posting("greenhouse", "203").repost_reason is None


async def test_a_copy_inside_the_fingerprint_window_still_merges(
    env: Env, gh: GreenhouseMock
) -> None:
    gh.respond(gh_job(101, published=OLD))
    await env.poll(sources=["greenhouse"])
    env.update_posting("greenhouse", "101", first_seen_at=NOW - timedelta(days=10))
    env.add_search()
    env.board.results = [[board_posting("9000", title="Other")], [board_posting("9001")]]
    await env.poll(sources=["linkedin"])
    await env.poll(sources=["linkedin"])

    copy = env.posting("linkedin", "9001")
    assert copy.tier == "merged"
    assert copy.repost_reason is None
    assert env.pushed == []


async def test_employment_type_is_stored_for_baseline_and_decided_postings(
    env: Env, gh: GreenhouseMock
) -> None:
    gh.respond(gh_job(101, published=OLD))
    await env.poll(sources=["greenhouse"])
    contract = gh_job(204, published=FRESH)
    contract["metadata"] = [{"id": 1, "name": "Employment Type", "value": "Contract"}]
    gh.respond(
        gh_job(101, published=OLD), contract, gh_job(205, "Platform Engineer", published=FRESH)
    )

    await env.poll(sources=["greenhouse"])

    assert env.posting("greenhouse", "101").employment_type == "full_time"
    assert env.posting("greenhouse", "204").employment_type == "contract"
    assert env.posting("greenhouse", "205").employment_type == "full_time"
    assert len(env.pushed) == 2  # a contract match is pushed like any other
