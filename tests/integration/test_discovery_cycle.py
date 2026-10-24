"""Item 9 (discovery): boards found in feed URLs are validated once and auto-added with their
own silent baseline."""

from datetime import timedelta

import httpx
import pytest
import respx
import time_machine

from jobpulse.db.session import session_scope
from jobpulse.db.tables import Company
from jobpulse.discovery import Discoverer
from jobpulse.sources.ats_detect import AtsDetector
from tests.factories import FIXTURES, NOW
from tests.integration.conftest import Env, SimplifyMock, lever_posting, simplify_listing

pytestmark = pytest.mark.respx(assert_all_called=False)

OLD = NOW - timedelta(days=3)
FRESH = NOW - timedelta(minutes=25)
DELTA_ID = "98959f08-1d1b-4b75-919e-833f825394b0"
DELTA_URL = f"https://jobs.lever.co/delta/{DELTA_ID}/apply"
DELTA_LIST = "https://api.lever.co/v0/postings/delta?mode=json"
ZETA_URL = "https://careers.zeta.example/careers/?gh_jid=5550001001#job-opening"
ZETA_PAGE = "https://careers.zeta.example/careers/?gh_jid=5550001001"
ZETA_LIST = "https://boards-api.greenhouse.io/v1/boards/zeta/jobs"


def delta_board() -> list[dict[str, object]]:
    return [
        lever_posting(DELTA_ID, "Software Engineer, Backend (New Grad)", created=OLD, token="delta")
    ]


async def feed_once(env: Env, feed: SimplifyMock, *listings: dict[str, object]) -> None:
    feed.respond(*listings, etag='W/"1"')
    await env.poll(sources=["simplify"])


async def test_a_board_seen_in_the_feed_is_validated_once_and_added(
    env: Env, feed: SimplifyMock, respx_mock: respx.MockRouter
) -> None:
    validation = respx_mock.get(DELTA_LIST).respond(200, json=delta_board())
    await feed_once(env, feed, simplify_listing("d1", url=DELTA_URL, posted=OLD))

    assert validation.call_count == 1
    delta = next(c for c in env.companies() if c.token == "delta")
    assert (delta.ats_type, delta.name, delta.region) == ("lever", "Delta", "us")
    assert delta.source_of_discovery == "auto"
    assert delta.priority == 0
    assert delta.active
    assert delta.baseline_done_at is None
    assert delta.discovered_from == DELTA_URL

    # Its first poll is a silent baseline; after that, new jobs are pushed.
    await env.poll(sources=["lever"], target_key="delta")
    assert env.notifier.sent == []
    assert env.posting("lever", DELTA_ID).tier == "baseline"
    respx_mock.get(DELTA_LIST).respond(
        200,
        json=[
            *delta_board(),
            lever_posting(
                "7f6e5d4c-3b2a-4190-8a7b-6c5d4e3f2a1b",
                "Backend Engineer",
                created=FRESH,
                token="delta",
            ),
        ],
    )
    await env.poll(sources=["lever"], target_key="delta")
    assert [message.title for message in env.notifier.sent] == [
        "Delta · Backend Engineer · SF · posted 25 min ago"
    ]


async def test_known_and_removed_companies_are_never_added_again(
    env: Env, feed: SimplifyMock
) -> None:
    with session_scope(env.sessions) as session:
        session.add(
            Company(
                name="Old Co",
                ats_type="ashby",
                token="OldCo",
                active=False,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    # Beta is on the watchlist; Old Co was deactivated. Neither may be validated (respx has
    # no routes for them, so a request would fail the test).
    await feed_once(
        env,
        feed,
        simplify_listing(
            "b1", url="https://jobs.lever.co/beta/5c6e2a0e-1f2b-4c3d-9e8f-0a1b2c3d4e5f", posted=OLD
        ),
        simplify_listing(
            "o1",
            url="https://jobs.ashbyhq.com/oldco/1b2c3d4e-5f60-4718-9a0b-1c2d3e4f5a6b",
            posted=OLD,
        ),
    )
    assert {c.token for c in env.companies()} == {"acme", "beta", "OldCo"}
    assert next(c for c in env.companies() if c.token == "OldCo").active is False


async def test_a_board_that_does_not_exist_is_not_retried_every_cycle(
    env: Env, feed: SimplifyMock, respx_mock: respx.MockRouter
) -> None:
    missing = respx_mock.get(DELTA_LIST).respond(
        404, json={"ok": False, "error": "Document not found"}
    )
    eu = respx_mock.get("https://api.eu.lever.co/v0/postings/delta?mode=json").respond(404)
    await feed_once(env, feed, simplify_listing("d1", url=DELTA_URL, posted=OLD))
    feed.respond(
        simplify_listing("d1", url=DELTA_URL, posted=OLD),
        simplify_listing("x", posted=OLD),
        etag='W/"2"',
    )
    await env.poll(sources=["simplify"])

    assert (missing.call_count, eu.call_count) == (1, 1)
    assert "delta" not in {c.token for c in env.companies()}


async def test_validations_are_capped_per_cycle(
    env: Env, feed: SimplifyMock, respx_mock: respx.MockRouter
) -> None:
    env.discoverer = Discoverer(AtsDetector(env.http), max_validations=1, max_page_fetches=0)
    routes = {
        token: respx_mock.get(f"https://api.lever.co/v0/postings/{token}?mode=json").respond(
            200, json=[]
        )
        for token in ("aaa", "bbb")
    }
    listings = [
        simplify_listing("l1", "Backend Engineer", url="https://jobs.lever.co/bbb", posted=OLD),
        simplify_listing("l2", "Sales Lead", url="https://jobs.lever.co/aaa", posted=OLD),
    ]
    feed.respond(*listings, etag='W/"1"')
    report = await env.poll(sources=["simplify"])
    assert (routes["bbb"].call_count, routes["aaa"].call_count) == (1, 0)  # the relevant one first
    assert report.discovery_deferred == 1

    feed.respond(*listings, simplify_listing("l3", posted=OLD), etag='W/"2"')
    await env.poll(sources=["simplify"])
    assert (routes["bbb"].call_count, routes["aaa"].call_count) == (1, 1)
    assert {"aaa", "bbb"} <= {c.token for c in env.companies()}


async def test_a_gh_jid_page_reveals_the_greenhouse_board(
    env: Env, feed: SimplifyMock, respx_mock: respx.MockRouter
) -> None:
    page = respx_mock.get(ZETA_PAGE).respond(
        200, text=(FIXTURES / "pages" / "zeta_careers.html").read_text(encoding="utf-8")
    )
    validation = respx_mock.get(ZETA_LIST).respond(
        200,
        json={
            "jobs": [{"id": 5550001001, "title": "Backend Engineer I", "company_name": "Zeta"}],
            "meta": {"total": 1},
        },
    )
    await feed_once(
        env,
        feed,
        simplify_listing("z1", "Backend Engineer I", company="Zeta", url=ZETA_URL, posted=OLD),
    )

    assert (page.call_count, validation.call_count) == (1, 1)
    zeta = next(c for c in env.companies() if c.token == "zeta")
    assert (zeta.ats_type, zeta.source_of_discovery, zeta.discovered_from) == (
        "greenhouse",
        "auto",
        ZETA_URL,
    )


async def test_a_dry_run_discovers_but_adds_nothing(
    env: Env, feed: SimplifyMock, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(DELTA_LIST).respond(200, json=delta_board())
    feed.respond(simplify_listing("d1", url=DELTA_URL, posted=OLD), etag='W/"1"')
    report = await env.poll(sources=["simplify"], dry_run=True)

    assert [line.split(" ")[0] for line in report.discovered] == ["lever/delta"]
    assert "delta" not in {c.token for c in env.companies()}


async def test_discovery_can_be_turned_off(env: Env, feed: SimplifyMock) -> None:
    env.discoverer = None
    await feed_once(env, feed, simplify_listing("d1", url=DELTA_URL, posted=OLD))
    assert "delta" not in {c.token for c in env.companies()}


async def test_an_unreachable_board_is_retried_after_a_while(
    env: Env,
    feed: SimplifyMock,
    respx_mock: respx.MockRouter,
    time_machine: time_machine.TimeMachineFixture,
) -> None:
    listing = simplify_listing("d1", url=DELTA_URL, posted=OLD)
    route = respx_mock.get(DELTA_LIST).mock(side_effect=httpx.ConnectError("refused"))
    await feed_once(env, feed, listing)
    attempts = route.call_count
    assert attempts >= 1
    assert "delta" not in {c.token for c in env.companies()}

    feed.respond(listing, simplify_listing("x1", posted=OLD), etag='W/"2"')
    await env.poll(sources=["simplify"])
    assert route.call_count == attempts  # remembered as a miss for a while

    time_machine.shift(timedelta(hours=3))
    route.side_effect = None
    route.return_value = httpx.Response(200, json=delta_board())
    feed.respond(listing, simplify_listing("x2", posted=OLD), etag='W/"3"')
    await env.poll(sources=["simplify"])
    assert "delta" in {c.token for c in env.companies()}


# --- narrowing ---------------------------------------------------------------------------------


async def test_only_boards_with_a_matching_posting_are_checked(
    env: Env, feed: SimplifyMock, respx_mock: respx.MockRouter
) -> None:
    # The sales board has no route: validating it would fail the test.
    wanted = respx_mock.get("https://api.lever.co/v0/postings/bbb?mode=json").respond(200, json=[])
    feed.respond(
        simplify_listing(
            "s1",
            "Account Executive",
            company="Sales Co",
            url="https://jobs.lever.co/aaa",
            posted=OLD,
        ),
        simplify_listing(
            "b1",
            "Backend Engineer",
            company="Builders",
            url="https://jobs.lever.co/bbb",
            posted=OLD,
        ),
        etag='W/"1"',
    )
    report = await env.poll(sources=["simplify"])
    assert wanted.call_count == 1
    assert {c.token for c in env.companies()} == {"acme", "beta", "bbb"}
    assert report.discovery_deferred == 0


async def test_a_location_filter_narrows_discovery_too(
    env: Env, feed: SimplifyMock, respx_mock: respx.MockRouter
) -> None:
    env.reconfigure(
        filters={**env.config.filters.model_dump(), "locations": {"include": ["^Remote"]}}
    )
    env.rebuild_discoverer()
    wanted = respx_mock.get("https://api.lever.co/v0/postings/remote-co?mode=json").respond(
        200, json=[]
    )
    feed.respond(
        simplify_listing(
            "l1",
            "Backend Engineer",
            url="https://jobs.lever.co/office-co",
            posted=OLD,
            locations=("Austin, TX",),
        ),
        simplify_listing(
            "l2",
            "Backend Engineer",
            url="https://jobs.lever.co/remote-co",
            posted=OLD,
            locations=("Remote in USA",),
        ),
        etag='W/"1"',
    )
    await env.poll(sources=["simplify"])
    assert wanted.call_count == 1
    assert "office-co" not in {c.token for c in env.companies()}


async def test_a_board_over_the_size_limit_is_reported_and_not_rechecked(
    env: Env, feed: SimplifyMock, respx_mock: respx.MockRouter
) -> None:
    env.reconfigure(discovery={**env.config.discovery.model_dump(), "max_board_jobs": 1})
    env.rebuild_discoverer()
    big = respx_mock.get(DELTA_LIST).respond(
        200,
        json=[
            *delta_board(),
            lever_posting(
                "7f6e5d4c-3b2a-4190-8a7b-6c5d4e3f2a1b",
                "Account Executive",
                created=OLD,
                token="delta",
            ),
        ],
    )
    feed.respond(simplify_listing("d1", url=DELTA_URL, posted=OLD), etag='W/"1"')
    report = await env.poll(sources=["simplify"])

    assert "delta" not in {c.token for c in env.companies()}
    assert report.discovery_skipped == [
        "lever/delta (Delta, 2 jobs): more than discovery.max_board_jobs (1)"
    ]
    feed.respond(
        simplify_listing("d1", url=DELTA_URL, posted=OLD),
        simplify_listing("x1", posted=OLD),
        etag='W/"2"',
    )
    await env.poll(sources=["simplify"])
    assert big.call_count == 1


async def test_discovery_stops_at_max_auto_companies(
    env: Env, feed: SimplifyMock, respx_mock: respx.MockRouter
) -> None:
    env.reconfigure(discovery={**env.config.discovery.model_dump(), "max_auto_companies": 1})
    env.add_company("Old Find", "ashby", "oldfind")  # already discovered and polled
    feed.respond(simplify_listing("d1", url=DELTA_URL, posted=OLD), etag='W/"1"')
    report = await env.poll(sources=["simplify"])

    assert "delta" not in {c.token for c in env.companies()}
    assert report.discovery_capped == 1
    assert len(respx_mock.calls) == 1  # the feed, and no validation
