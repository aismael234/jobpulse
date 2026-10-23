"""The SimplifyJobs feed end to end: its own baseline, the persisted ETag, closures from
``active: false``, and dedupe against watched ATS boards."""

from datetime import timedelta

import httpx
import pytest

from jobpulse.core.dedupe import posting_fingerprint
from jobpulse.db import repo
from tests.factories import NOW, make_posting
from tests.integration.conftest import Env, GreenhouseMock, SimplifyMock, gh_job, simplify_listing

pytestmark = pytest.mark.respx(assert_all_called=False)

OLD = NOW - timedelta(days=3)
FRESH = NOW - timedelta(minutes=25)
ETAG_1 = 'W/"4ad2fe91"'
ETAG_2 = 'W/"88b39eb0"'
DELTA_PUSH = "Delta · Backend Engineer, New Grad · SF · posted 25 min ago"


async def baseline(env: Env, feed: SimplifyMock, *listing_ids: str) -> None:
    # Distinct titles: the same company, title and city would fingerprint-merge (rule 5c).
    feed.respond(
        *(simplify_listing(i, f"Platform Engineer {i}", posted=OLD) for i in listing_ids),
        etag=ETAG_1,
    )
    await env.poll(sources=["simplify"])


async def test_the_first_successful_fetch_is_a_silent_baseline(
    env: Env, feed: SimplifyMock
) -> None:
    feed.respond(
        simplify_listing("a1", posted=FRESH),
        simplify_listing("a2", posted=NOW - timedelta(minutes=5)),
        simplify_listing("gone", posted=OLD, active=False),
        etag=ETAG_1,
    )
    report = await env.poll(sources=["simplify"])

    assert env.notifier.sent == []
    assert sorted(row.external_id for row in env.postings()) == ["a1", "a2"]
    assert {row.tier for row in env.postings()} == {"baseline"}
    [target] = report.targets
    assert (target.source, target.key) == ("simplify", "new-grad")
    assert target.is_baseline
    assert target.baseline_stored == 2
    state = env.feed_state()
    assert state.etag == ETAG_1
    assert state.baseline_done_at == NOW
    assert feed.sent_etag() is None


async def test_a_failed_first_fetch_is_not_the_baseline(env: Env, feed: SimplifyMock) -> None:
    feed.fail_with(httpx.Response(503))
    await env.poll(sources=["simplify"])
    assert env.postings() == []
    assert env.feed_state().baseline_done_at is None
    assert env.feed_state().etag is None
    [run] = env.runs()
    assert (run.ok, run.error_kind) == (False, "http_5xx")

    feed.respond(simplify_listing("a1", posted=FRESH), etag=ETAG_1)
    await env.poll(sources=["simplify"])
    assert env.notifier.sent == []
    assert [row.tier for row in env.postings()] == ["baseline"]


async def test_a_fresh_new_listing_is_pushed_once(env: Env, feed: SimplifyMock) -> None:
    await baseline(env, feed, "a1")
    feed.respond(
        simplify_listing("a1", posted=OLD), simplify_listing("a2", posted=FRESH), etag=ETAG_2
    )
    report = await env.poll(sources=["simplify"])

    assert feed.sent_etag() == ETAG_1
    assert report.pushes == [DELTA_PUSH]
    assert [message.title for message in env.notifier.sent] == [DELTA_PUSH]
    assert env.posting("simplify", "a2").tier == "instant"
    assert env.feed_state().etag == ETAG_2

    await env.poll(sources=["simplify"])
    assert len(env.notifier.sent) == 1


async def test_an_old_listing_new_to_the_feed_is_stale(env: Env, feed: SimplifyMock) -> None:
    await baseline(env, feed, "a1")
    feed.respond(
        simplify_listing("a1", posted=OLD),
        simplify_listing("a2", posted=NOW - timedelta(days=10)),
        etag=ETAG_2,
    )
    await env.poll(sources=["simplify"])
    assert env.notifier.sent == []
    assert env.posting("simplify", "a2").tier == "stale"


async def test_a_304_is_an_unchanged_success(env: Env, feed: SimplifyMock) -> None:
    await baseline(env, feed, "a1", "a2")
    feed.not_modified()
    report = await env.poll(sources=["simplify"])

    assert feed.sent_etag() == ETAG_1
    [target] = report.targets
    assert target.ok
    assert target.not_modified
    last = env.runs()[-1]
    assert (last.ok, last.not_modified, last.fetched_count) == (True, True, 0)
    assert all(row.closed_at is None for row in env.postings())
    assert env.feed_state().etag == ETAG_1


async def test_listings_close_on_active_false_and_reopen(env: Env, feed: SimplifyMock) -> None:
    await baseline(env, feed, "a1", "a2")
    feed.respond(
        simplify_listing("a1", posted=OLD),
        simplify_listing("a2", posted=OLD, active=False),
        etag=ETAG_2,
    )
    report = await env.poll(sources=["simplify"])
    assert report.targets[0].closed == 1
    assert env.posting("simplify", "a2").closed_at == NOW
    assert env.posting("simplify", "a1").closed_at is None

    feed.respond(
        simplify_listing("a1", posted=OLD), simplify_listing("a2", posted=OLD), etag='W/"3"'
    )
    report = await env.poll(sources=["simplify"])
    assert report.targets[0].reopened == 1
    assert env.posting("simplify", "a2").closed_at is None
    assert env.notifier.sent == []


async def test_listings_missing_from_the_feed_are_never_closed(
    env: Env, feed: SimplifyMock
) -> None:
    await baseline(env, feed, "a1", "a2")
    feed.respond(simplify_listing("a1", posted=OLD), etag=ETAG_2)
    for _ in range(3):
        await env.poll(sources=["simplify"])
    gone = env.posting("simplify", "a2")
    assert gone.closed_at is None
    assert gone.missing_count == 0


async def test_hidden_listings_are_neither_stored_nor_closed(env: Env, feed: SimplifyMock) -> None:
    await baseline(env, feed, "a1")
    feed.respond(
        simplify_listing("a1", posted=OLD, visible=False),
        simplify_listing("a2", posted=FRESH, visible=False),
        etag=ETAG_2,
    )
    await env.poll(sources=["simplify"])
    assert [row.external_id for row in env.postings()] == ["a1"]
    assert env.posting("simplify", "a1").closed_at is None
    assert env.notifier.sent == []


async def test_a_dry_run_leaves_the_etag_alone(env: Env, feed: SimplifyMock) -> None:
    await baseline(env, feed, "a1")
    feed.respond(
        simplify_listing("a1", posted=OLD), simplify_listing("a2", posted=FRESH), etag=ETAG_2
    )
    report = await env.poll(sources=["simplify"], dry_run=True)
    assert report.pushes == [DELTA_PUSH]
    assert env.feed_state().etag == ETAG_1
    assert len(env.postings()) == 1


async def test_a_feed_copy_of_a_watched_ats_job_is_merged(
    env: Env, gh: GreenhouseMock, feed: SimplifyMock
) -> None:
    gh.respond(gh_job(101, published=OLD))
    await env.poll(sources=["greenhouse"])
    await baseline(env, feed)
    feed.respond(
        simplify_listing(
            "s1",
            "Backend Engineer",
            company="Acme",
            url="https://job-boards.greenhouse.io/acme/jobs/101",
            posted=FRESH,
        ),
        etag=ETAG_2,
    )
    report = await env.poll(sources=["simplify"])

    assert env.notifier.sent == []
    assert report.targets[0].merged == 1
    copy = env.posting("simplify", "s1")
    original = env.posting("greenhouse", "101")
    assert copy.merged_into_id == original.id
    assert [entry["source"] for entry in original.also_seen_on] == ["simplify"]


async def test_a_pending_listing_is_finished_even_when_the_feed_is_unchanged(
    env: Env, feed: SimplifyMock
) -> None:
    await baseline(env, feed, "a1")
    interrupted = make_posting(
        source="simplify",
        external_id="a2",
        company_name="Delta",
        company_token=None,
        title="Backend Engineer, New Grad",
        url="https://jobs.example.com/delta/a2",
        canonical_url="https://jobs.example.com/delta/a2",
        posted_at=FRESH,
    )
    with env.sessions() as session:
        repo.insert_if_absent(
            session,
            interrupted,
            company_id=None,
            fingerprint=posting_fingerprint(interrupted),
            tier="pending",
            tier_reason=None,
            now=NOW - timedelta(minutes=10),
        )
        session.commit()

    feed.not_modified()
    await env.poll(sources=["simplify"])
    assert env.posting("simplify", "a2").tier == "instant"
    assert [message.title for message in env.notifier.sent] == [DELTA_PUSH]
