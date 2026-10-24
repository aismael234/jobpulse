"""The company blocklist end to end: blocked companies are never pushed, never discovered, and
their auto-added boards aren't polled; blocked boards are never added or polled."""

from datetime import timedelta

import pytest
import respx

from tests.factories import NOW, jobspy_frame, jobspy_row
from tests.integration.conftest import (
    LEVER_LIST,
    Env,
    ScriptedScrape,
    SimplifyMock,
    lever_posting,
    make_config,
    simplify_listing,
)

pytestmark = pytest.mark.respx(assert_all_called=False)

OLD = NOW - timedelta(days=3)
FRESH = NOW - timedelta(minutes=25)
DELTA_ID = "98959f08-1d1b-4b75-919e-833f825394b0"
DELTA_URL = f"https://jobs.lever.co/delta/{DELTA_ID}/apply"
DELTA_LIST = "https://api.lever.co/v0/postings/delta?mode=json"
BLOCKED = "company is on the blocklist"


def block(env: Env, *, companies: tuple[str, ...] = (), boards: tuple[str, ...] = ()) -> None:
    env.reconfigure(blocklist={"companies": list(companies), "boards": list(boards)})


async def test_a_blocked_company_is_stored_but_never_pushed(
    env: Env, boards: ScriptedScrape
) -> None:
    block(env, companies=("Jobot",))
    env.add_search()
    boards.queue(
        "linkedin",
        jobspy_frame([]),
        jobspy_frame(
            [
                jobspy_row("linkedin", external_id="4101000021", company="Jobot, Inc."),
                jobspy_row(
                    "linkedin",
                    external_id="4101000022",
                    company="Northwind",
                    location="Seattle, WA, USA",
                ),
            ]
        ),
    )
    await env.poll(sources=["linkedin"])
    report = await env.poll(sources=["linkedin"])

    blocked = env.posting("linkedin", "4101000021")
    assert (blocked.tier, blocked.tier_reason) == ("filtered", BLOCKED)
    assert [message.title for message in env.notifier.sent] == [
        "Northwind · Backend Engineer · Seattle · posted today"
    ]
    assert report.targets[0].filtered == 1


async def test_a_blocked_company_in_the_feed_is_not_discovered(
    env: Env, feed: SimplifyMock
) -> None:
    block(env, companies=("Delta",))
    feed.respond(etag='W/"1"')
    await env.poll(sources=["simplify"])
    # No route for Delta's board: checking it would fail the test.
    feed.respond(simplify_listing("d1", url=DELTA_URL, posted=FRESH), etag='W/"2"')
    await env.poll(sources=["simplify"])

    assert "delta" not in {c.token for c in env.companies()}
    assert env.posting("simplify", "d1").tier == "filtered"
    assert env.notifier.sent == []


async def test_a_blocked_board_is_not_discovered_but_its_jobs_still_count(
    env: Env, feed: SimplifyMock
) -> None:
    block(env, boards=("lever/delta",))
    feed.respond(etag='W/"1"')
    await env.poll(sources=["simplify"])
    feed.respond(simplify_listing("d1", url=DELTA_URL, posted=FRESH), etag='W/"2"')
    await env.poll(sources=["simplify"])

    assert "delta" not in {c.token for c in env.companies()}
    assert len(env.notifier.sent) == 1  # the job itself isn't blocked, only the board


@pytest.mark.parametrize(
    "blocklist",
    [{"companies": ("Delta",)}, {"boards": ("lever/delta",)}],
    ids=["by-name", "by-board"],
)
async def test_a_blocked_auto_added_board_is_not_polled_until_unblocked(
    env: Env, respx_mock: respx.MockRouter, blocklist: dict[str, tuple[str, ...]]
) -> None:
    respx_mock.get(LEVER_LIST).respond(200, json=[])
    delta = respx_mock.get(DELTA_LIST).respond(
        200, json=[lever_posting(DELTA_ID, created=OLD, token="delta")]
    )
    env.add_company("Delta", "lever", "delta")
    block(env, **blocklist)
    report = await env.poll(sources=["lever"])
    assert delta.call_count == 0
    assert [target.key for target in report.targets] == ["beta"]

    block(env)
    await env.poll(sources=["lever"])
    assert delta.call_count == 1


async def test_blocked_auto_companies_leave_room_under_the_cap(
    env: Env, feed: SimplifyMock, respx_mock: respx.MockRouter
) -> None:
    env.config = make_config(
        discovery={"max_auto_companies": 1}, blocklist={"companies": ["Old Find"]}
    )
    env.add_company("Old Find", "ashby", "oldfind")
    validation = respx_mock.get(DELTA_LIST).respond(
        200, json=[lever_posting(DELTA_ID, created=OLD, token="delta")]
    )
    feed.respond(simplify_listing("d1", url=DELTA_URL, posted=OLD), etag='W/"1"')
    await env.poll(sources=["simplify"])
    assert validation.call_count == 1
    assert "delta" in {c.token for c in env.companies()}


async def test_a_board_whose_official_name_is_blocked_is_not_added(
    env: Env, feed: SimplifyMock, respx_mock: respx.MockRouter
) -> None:
    block(env, companies=("Zeta Holdings",))
    validation = respx_mock.get("https://boards-api.greenhouse.io/v1/boards/zeta/jobs").respond(
        200,
        json={"jobs": [{"id": 1, "title": "Backend Engineer", "company_name": "Zeta Holdings"}]},
    )
    listing = simplify_listing(
        "z1",
        "Backend Engineer",
        company="Zeta",
        url="https://job-boards.greenhouse.io/zeta/jobs/1",
        posted=OLD,
    )
    feed.respond(listing, etag='W/"1"')
    await env.poll(sources=["simplify"])
    feed.respond(listing, simplify_listing("x1", posted=OLD), etag='W/"2"')
    await env.poll(sources=["simplify"])

    assert "zeta" not in {c.token for c in env.companies()}
    assert validation.call_count == 1  # remembered, not checked every cycle


async def test_a_waiting_push_for_a_newly_blocked_company_is_dropped(
    env: Env, boards: ScriptedScrape
) -> None:
    env.add_search()
    row = jobspy_row(
        "linkedin", external_id="4101000031", company="Northwind", location="Seattle, WA, USA"
    )
    boards.queue("linkedin", jobspy_frame([]), jobspy_frame([row]), jobspy_frame([row]))
    await env.poll(sources=["linkedin"])
    env.notifier.failures_left = 1
    await env.poll(sources=["linkedin"])  # decided instant, but the push failed
    assert env.posting("linkedin", "4101000031").notified_at is None

    block(env, companies=("Northwind",))
    await env.poll(sources=["linkedin"])
    assert env.notifier.sent == []
    dropped = env.posting("linkedin", "4101000031")
    assert (dropped.tier, dropped.tier_reason) == ("filtered", BLOCKED)
