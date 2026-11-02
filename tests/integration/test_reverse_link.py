"""The reverse link: an ATS posting that shows up after a job-board or feed copy of the same
job adopts that copy (and its alert state), so the job is never pushed twice."""

from datetime import timedelta

import pytest
import respx

from tests.factories import NOW, jobspy_frame, jobspy_row
from tests.integration.conftest import (
    Env,
    GreenhouseMock,
    ScriptedScrape,
    SimplifyMock,
    gh_job,
    lever_posting,
    simplify_listing,
)

pytestmark = pytest.mark.respx(assert_all_called=False)

OLD = NOW - timedelta(days=3)
FRESH = NOW - timedelta(minutes=25)
DELTA_ID = "98959f08-1d1b-4b75-919e-833f825394b0"
DELTA_LIST = "https://api.lever.co/v0/postings/delta?mode=json"


async def test_an_ats_job_first_seen_on_linkedin_is_not_pushed_twice(
    env: Env, gh: GreenhouseMock, boards: ScriptedScrape
) -> None:
    gh.respond(gh_job(101, published=OLD))
    await env.poll(sources=["greenhouse"])
    env.add_search()
    copy = jobspy_row("linkedin", external_id="4101000010", title="Backend Engineer, Data")
    boards.queue("linkedin", jobspy_frame([]), jobspy_frame([copy]))
    await env.poll(sources=["linkedin"])
    await env.poll(sources=["linkedin"])
    assert len(env.notifier.sent) == 1

    gh.respond(
        gh_job(101, published=OLD),
        gh_job(102, "Backend Engineer, Data", published=FRESH, location="San Francisco, CA"),
    )
    await env.poll(sources=["greenhouse"])

    assert len(env.notifier.sent) == 1
    board_row = env.posting("linkedin", "4101000010")
    ats_row = env.posting("greenhouse", "102")
    assert board_row.merged_into_id == ats_row.id
    assert board_row.tier == "merged"
    assert ats_row.merged_into_id is None
    assert ats_row.notified_at == board_row.notified_at == NOW
    assert ats_row.tier == "instant"
    assert [(e["source"], e["external_id"], e["matched_by"]) for e in ats_row.also_seen_on] == [
        ("linkedin", "4101000010", "fingerprint")
    ]

    # Later copies land on the ATS row, not on the old LinkedIn row.
    env.add_search("backend-remote", "indeed")
    indeed_copy = jobspy_row(
        "indeed",
        title="Backend Engineer, Data",
        job_url_direct="https://job-boards.greenhouse.io/acme/jobs/102?gh_src=x9",
    )
    boards.queue("indeed", jobspy_frame([]), jobspy_frame([indeed_copy]))
    await env.poll(sources=["indeed"])
    await env.poll(sources=["indeed"])
    assert env.posting("indeed", indeed_copy["id"].removeprefix("in-")).merged_into_id == ats_row.id
    assert len(env.notifier.sent) == 1


async def test_copies_merged_into_the_adopted_row_follow_it(
    env: Env, gh: GreenhouseMock, boards: ScriptedScrape
) -> None:
    gh.respond(gh_job(101, published=OLD))
    await env.poll(sources=["greenhouse"])
    env.add_search()
    env.add_search("backend-remote", "indeed")
    boards.queue(
        "linkedin",
        jobspy_frame([]),
        jobspy_frame([jobspy_row("linkedin", title="Backend Engineer, Data")]),
    )
    boards.queue(
        "indeed",
        jobspy_frame([]),
        jobspy_frame([jobspy_row("indeed", title="Backend Engineer, Data")]),
    )
    await env.poll(sources=["linkedin", "indeed"])
    await env.poll(sources=["linkedin", "indeed"])
    linkedin_row = env.posting("linkedin", "4101234567")
    indeed_row = env.posting("indeed", "1a2b3c4d5e6f7a8b")
    assert indeed_row.merged_into_id == linkedin_row.id  # fingerprint, among the copies

    gh.respond(
        gh_job(101, published=OLD),
        gh_job(102, "Backend Engineer, Data", published=FRESH, location="San Francisco, CA"),
    )
    await env.poll(sources=["greenhouse"])

    ats_row = env.posting("greenhouse", "102")
    assert env.posting("linkedin", "4101234567").merged_into_id == ats_row.id
    assert env.posting("indeed", "1a2b3c4d5e6f7a8b").merged_into_id == ats_row.id
    assert {e["source"] for e in ats_row.also_seen_on} == {"linkedin", "indeed"}
    assert len(env.notifier.sent) == 1


async def test_a_new_board_baseline_takes_over_a_pushed_feed_listing(
    env: Env, feed: SimplifyMock, respx_mock: respx.MockRouter
) -> None:
    feed.respond(etag='W/"1"')
    await env.poll(sources=["simplify"])
    listing = simplify_listing(
        "s1",
        "Software Engineer, Backend (New Grad)",
        url=f"https://jobs.lever.co/delta/{DELTA_ID}/apply",
        posted=FRESH,
    )
    respx_mock.get(DELTA_LIST).respond(
        200,
        json=[
            lever_posting(
                DELTA_ID, "Software Engineer, Backend (New Grad)", created=FRESH, token="delta"
            ),
            lever_posting(
                "0b7f3c2e-9d14-4a6b-8e5f-3c1d2a4b6e8f", "Sales Engineer", created=OLD, token="delta"
            ),
        ],
    )
    feed.respond(listing, etag='W/"2"')
    await env.poll(sources=["simplify"])
    assert len(env.notifier.sent) == 1
    pushed = env.posting("simplify", "s1")
    assert pushed.tier == "instant"

    # Discovery added Delta (it validated the board); its first poll is a silent baseline.
    await env.poll(sources=["lever"], target_key="delta")
    assert len(env.notifier.sent) == 1
    ats_row = env.posting("lever", DELTA_ID)
    assert env.posting("simplify", "s1").merged_into_id == ats_row.id
    assert ats_row.tier == "instant"  # the baseline row took over the feed's match
    assert ats_row.notified_at == pushed.notified_at
    assert ats_row.score == pushed.score
    assert env.posting("lever", "0b7f3c2e-9d14-4a6b-8e5f-3c1d2a4b6e8f").tier == "baseline"


async def test_without_a_match_the_baseline_stays_silent(
    env: Env, gh: GreenhouseMock, boards: ScriptedScrape
) -> None:
    env.pause("acme", "beta")
    env.add_search()
    boards.queue(
        "linkedin",
        jobspy_frame([]),
        jobspy_frame([jobspy_row("linkedin", title="Backend Engineer, Ledger")]),
    )
    await env.poll(sources=["linkedin"])
    await env.poll(sources=["linkedin"])
    assert len(env.notifier.sent) == 1

    env.pause("acme", "beta", paused=False)
    gh.respond(
        gh_job(101, "Backend Engineer, Payroll", published=FRESH, location="San Francisco, CA")
    )
    await env.poll(sources=["greenhouse"])
    assert env.posting("greenhouse", "101").tier == "baseline"
    assert env.posting("linkedin", "4101234567").merged_into_id is None
    assert len(env.notifier.sent) == 1
