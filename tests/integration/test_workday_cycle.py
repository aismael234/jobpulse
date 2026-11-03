"""M4: Workday boards through the whole cycle: the silent baseline, the detail call for new
survivors, closures only for a board read in full, keyword changes, and feed copies of a
Workday job merging onto (or being taken over by) the Workday row."""

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx

from tests.factories import NOW
from tests.integration.conftest import Env, SimplifyMock, simplify_listing

TOKEN = "omega.wd5/External"
API = "https://omega.wd5.myworkdayjobs.com/wday/cxs/omega/External"
JOBS_URL = f"{API}/jobs"
DETAIL = re.compile(re.escape(API) + r"/job/[^/]+/(?P<job>[^/?]+)")
NEW_GRAD = "Backend-Engineer-New-Grad_R0012345"
NEW_GRAD_URL = f"https://omega.wd5.myworkdayjobs.com/External/job/Seattle-WA/{NEW_GRAD}"


def wd_job(number: int, title: str = "Backend Engineer", **fields: Any) -> dict[str, Any]:
    slug = title.replace(",", "").replace(" ", "-")
    return {
        "title": title,
        "externalPath": f"/job/US-WA-Seattle/{slug}_R{number:07d}",
        "locationsText": "US, WA, Seattle",
        "postedOn": "Posted Today",
        "bulletFields": [f"R{number:07d}"],
        **fields,
    }


def external_id(job: dict[str, Any]) -> str:
    return f"omega.wd5/external/{job['externalPath'].rsplit('/', 1)[-1].lower()}"


@dataclass
class WorkdayMock:
    """Omega's Workday board: set ``jobs`` (or ``by_keyword``) per poll; details answer from
    ``details`` (by job id), else with a default description."""

    list_route: respx.Route
    detail_route: respx.Route
    jobs: list[dict[str, Any]] = field(default_factory=list)
    by_keyword: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    details: dict[str, dict[str, Any]] = field(default_factory=dict)

    def answer_list(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["limit"] <= 20
        jobs = self.by_keyword.get(body["searchText"], []) if body["searchText"] else self.jobs
        offset = body["offset"]
        return httpx.Response(
            200,
            json={
                "total": len(jobs) if offset == 0 else 0,
                "jobPostings": jobs[offset : offset + body["limit"]],
            },
        )

    def answer_detail(self, request: httpx.Request, job: str) -> httpx.Response:
        """respx passes the URL pattern's named group ``job``."""
        info = {
            "title": "Backend Engineer",
            "jobDescription": "<p>Build Python services. 3+ years of experience.</p>",
            "location": "US, WA, Seattle",
            "startDate": "2026-10-23",
            "jobReqId": job.rsplit("_", 1)[-1],
            "jobPostingId": job,
            **self.details.get(job, {}),
        }
        return httpx.Response(200, json={"jobPostingInfo": info})


@pytest.fixture
def workday(env: Env, respx_mock: respx.MockRouter) -> WorkdayMock:
    env.pause("acme", "beta")
    env.watch("Omega", "workday", TOKEN, priority=5)
    mock = WorkdayMock(respx_mock.post(JOBS_URL), respx_mock.get(url__regex=DETAIL.pattern))
    mock.list_route.side_effect = mock.answer_list
    mock.detail_route.side_effect = mock.answer_detail
    return mock


def keywords(env: Env, *words: str) -> None:
    env.set_keywords(TOKEN, words)


async def test_the_first_poll_is_silent_and_a_new_job_is_pushed(
    env: Env, workday: WorkdayMock
) -> None:
    workday.jobs = [wd_job(1), wd_job(2, "Platform Engineer")]
    first = await env.poll(sources=["workday"])

    [report] = first.targets
    assert (report.source, report.key) == ("workday", TOKEN)
    assert report.is_baseline
    assert report.baseline_stored == 2
    assert workday.detail_route.call_count == 0
    assert env.notifier.sent == []
    assert env.company(TOKEN).baseline_done_at is not None

    workday.jobs.append(wd_job(3, "Backend Engineer, Search"))
    await env.poll(sources=["workday"])

    assert workday.detail_route.call_count == 1  # only the new survivor
    [push] = env.pushed
    assert push == "Omega · Backend Engineer, Search · Seattle · posted today"
    row = env.posting("workday", external_id(wd_job(3, "Backend Engineer, Search")))
    assert row.tier == "instant"
    assert row.description_text is not None
    assert "Build Python services" in row.description_text
    assert row.canonical_url == "workday:omega.wd5/external:backend-engineer-search_r0000003"


async def test_a_new_job_posted_long_ago_is_stale_without_a_detail_call(
    env: Env, workday: WorkdayMock
) -> None:
    workday.jobs = [wd_job(1)]
    await env.poll(sources=["workday"])
    workday.jobs.append(wd_job(4, "Backend Engineer, Old", postedOn="Posted 30+ Days Ago"))
    await env.poll(sources=["workday"])

    assert env.posting("workday", external_id(workday.jobs[1])).tier == "stale"
    assert workday.detail_route.call_count == 0
    assert env.notifier.sent == []


async def test_a_board_read_in_full_closes_missing_jobs(env: Env, workday: WorkdayMock) -> None:
    kept, dropped = wd_job(1), wd_job(2, "Platform Engineer")
    workday.jobs = [kept, dropped]
    await env.poll(sources=["workday"])
    workday.jobs = [kept]
    await env.poll(sources=["workday"])
    assert env.posting("workday", external_id(dropped)).closed_at is None  # one miss
    await env.poll(sources=["workday"])
    assert env.posting("workday", external_id(dropped)).closed_at is not None

    workday.jobs = [kept, dropped]
    await env.poll(sources=["workday"])
    reopened = env.posting("workday", external_id(dropped))
    assert (reopened.closed_at, reopened.missing_count) == (None, 0)


async def test_a_board_searched_by_keyword_never_closes_anything(
    env: Env, workday: WorkdayMock
) -> None:
    keywords(env, "backend")
    kept, dropped = wd_job(1), wd_job(2, "Backend Engineer II")
    workday.by_keyword = {"backend": [kept, dropped]}
    await env.poll(sources=["workday"])
    workday.by_keyword = {"backend": [kept]}
    for _ in range(3):
        await env.poll(sources=["workday"])

    row = env.posting("workday", external_id(dropped))
    assert (row.closed_at, row.missing_count) == (None, 0)
    searched = {json.loads(call.request.content)["searchText"] for call in workday.list_route.calls}
    assert searched == {"backend"}


async def test_changed_keywords_start_a_new_silent_baseline(env: Env, workday: WorkdayMock) -> None:
    keywords(env, "backend")
    workday.by_keyword = {"backend": [wd_job(1)], "platform": [wd_job(5, "Platform Engineer")]}
    await env.poll(sources=["workday"])
    keywords(env, "backend", "platform")
    second = await env.poll(sources=["workday"])

    [report] = second.targets
    assert report.is_baseline
    assert report.baseline_stored == 1
    assert env.notifier.sent == []
    assert env.posting("workday", external_id(wd_job(5, "Platform Engineer"))).tier == "baseline"


async def test_a_feed_copy_of_a_watched_workday_job_is_merged(
    env: Env, workday: WorkdayMock, feed: SimplifyMock
) -> None:
    job = wd_job(9, "Backend Engineer, New Grad", externalPath=f"/job/Seattle-WA/{NEW_GRAD}")
    workday.jobs = [job]
    await env.poll(sources=["workday"])
    feed.respond(simplify_listing("unrelated-1", "Backend Engineer", company="Delta", posted=NOW))
    await env.poll(sources=["simplify"])  # the feed's own silent baseline

    copy = simplify_listing(
        "omega-copy",
        "Backend Engineer, New Grad",
        company="Omega Systems",
        url=NEW_GRAD_URL,
        posted=NOW,
    )
    feed.respond(copy, etag='"v2"')
    report = await env.poll(sources=["simplify"])

    [target] = report.targets
    assert target.merged == 1
    primary = env.posting("workday", external_id(job))
    duplicate = env.posting("simplify", "omega-copy")
    assert duplicate.merged_into_id == primary.id
    assert [entry["source"] for entry in primary.also_seen_on] == ["simplify"]
    assert env.notifier.sent == []


async def test_a_new_workday_board_takes_over_an_earlier_feed_copy(
    env: Env, workday: WorkdayMock, feed: SimplifyMock
) -> None:
    env.pause(TOKEN)
    feed.respond(simplify_listing("unrelated-1", "Backend Engineer", company="Delta", posted=NOW))
    await env.poll(sources=["simplify"])
    copy = simplify_listing(
        "omega-copy",
        "Backend Engineer, New Grad",
        company="Omega Systems",
        url=NEW_GRAD_URL,
        posted=NOW,
    )
    feed.respond(copy, etag='"v2"')
    await env.poll(sources=["simplify"])
    assert len(env.notifier.sent) == 1  # pushed from the feed

    env.pause(TOKEN, paused=False)
    job = wd_job(9, "Backend Engineer, New Grad", externalPath=f"/job/Seattle-WA/{NEW_GRAD}")
    workday.jobs = [job]
    report = await env.poll(sources=["workday"])

    [target] = report.targets
    assert target.is_baseline
    assert target.adopted == 1
    primary = env.posting("workday", external_id(job))
    assert env.posting("simplify", "omega-copy").merged_into_id == primary.id
    assert primary.notified_at is not None  # the push already happened, from the feed
    assert len(env.notifier.sent) == 1


async def test_jobs_in_several_places_are_judged_after_the_detail_call(
    env: Env, workday: WorkdayMock
) -> None:
    filters = env.config.filters.model_dump()
    filters["locations"] = {"include": ["Seattle"], "unknown_location_passes": False}
    env.reconfigure(filters=filters)
    workday.jobs = [wd_job(1)]
    await env.poll(sources=["workday"])

    near = wd_job(2, "Backend Engineer, Near", locationsText="2 Locations")
    far = wd_job(3, "Backend Engineer, Far", locationsText="2 Locations")
    workday.details = {
        near["externalPath"].rsplit("/", 1)[-1]: {
            "location": "US, WA, Seattle",
            "additionalLocations": ["US, CA, San Jose"],
        },
        far["externalPath"].rsplit("/", 1)[-1]: {
            "location": "US, TX, Austin",
            "additionalLocations": ["US, CO, Denver"],
        },
    }
    workday.jobs += [near, far]
    await env.poll(sources=["workday"])

    assert workday.detail_route.call_count == 2  # neither was filtered before its details
    near_row = env.posting("workday", external_id(near))
    far_row = env.posting("workday", external_id(far))
    assert near_row.tier == "instant"
    assert near_row.locations == ["Seattle, WA", "San Jose, CA"]
    assert far_row.tier == "filtered"
    assert far_row.locations == ["Austin, TX", "Denver, CO"]


async def test_a_job_posted_three_days_ago_is_stale_from_the_list_alone(
    env: Env, workday: WorkdayMock
) -> None:
    """Day precision counts from the end of the posted day: 3 days ago is 62 h old at 14:00."""
    workday.jobs = [wd_job(1)]
    await env.poll(sources=["workday"])
    workday.jobs.append(wd_job(2, "Backend Engineer Two", postedOn="Posted 3 Days Ago"))
    await env.poll(sources=["workday"])

    row = env.posting("workday", external_id(workday.jobs[1]))
    assert row.tier == "stale"
    assert row.posted_at is not None
    assert row.posted_at.date() == (NOW - timedelta(days=3)).date()
    assert row.posted_at_precision == "day"
    assert workday.detail_route.call_count == 0
