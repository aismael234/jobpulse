"""M4: LinkedIn's public job page. The applicant line in its phrasings, the description with
its criteria, pages one at a time with pauses, and a 429 that ends job pages for the cycle and
cools LinkedIn searches down too. A page that couldn't be read raises EnrichmentDeferred, and
pages queued behind the cap or a cool-down don't sleep a pause each."""

import asyncio
import random
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from jobpulse.core.applicants import ApplicantCount
from jobpulse.core.models import FetchResult, Posting, SearchTarget
from jobpulse.httpclient import HttpClient
from jobpulse.sources.base import EnrichmentDeferred
from jobpulse.sources.jobspy_boards import JobSpyAdapter
from jobpulse.sources.linkedin_gate import LinkedInGate, Pacing
from jobpulse.sources.linkedin_guest import (
    JOB_PAGE_URL,
    JobPage,
    LinkedInJobPages,
    LinkedInSource,
    parse_job_page,
)
from tests.factories import FIXTURES, NOW, FakeMonotonic, FakeSleep, jobspy_frame, make_posting

PAGE_25 = (FIXTURES / "linkedin" / "job_4101100003.html").read_text(encoding="utf-8")
PAGE_200 = (FIXTURES / "linkedin" / "job_4101100004.html").read_text(encoding="utf-8")
SEARCH = SearchTarget(search_id=1, site="linkedin", name="backend-sf", search_term="backend")


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def linkedin_posting(job_id: str = "4101100003", **overrides: Any) -> Posting:
    values: dict[str, Any] = {
        "source": "linkedin",
        "external_id": job_id,
        "company_name": "Northwind",
        "company_token": None,
        "url": f"https://www.linkedin.com/jobs/view/{job_id}",
        "canonical_url": f"https://www.linkedin.com/jobs/view/{job_id}",
        "posted_at": None,
        "locations": ("Seattle, WA",),
        **overrides,
    }
    return make_posting(**values)


def page_url(job_id: str) -> str:
    return JOB_PAGE_URL.format(job_id=job_id)


class Setup:
    def __init__(self, http: HttpClient, *, max_per_hour: int = 40) -> None:
        self.sleep = FakeSleep()
        self.monotonic = FakeMonotonic()
        self.clock = Clock()
        self.gate = LinkedInGate(
            Pacing(delay_min=4.0, delay_max=8.0, cooldown=timedelta(minutes=30)),
            clock=self.clock,
            sleep=self.sleep,
            rng=random.Random(3),
            monotonic=self.monotonic,
        )
        self.pages = LinkedInJobPages(
            http, self.gate, max_per_hour=max_per_hour, monotonic=self.monotonic
        )


# --- parsing -------------------------------------------------------------------------------


def test_parses_the_applicant_line_and_the_description() -> None:
    page = parse_job_page(PAGE_25)
    assert page.applicants == ApplicantCount("Be among the first 25 applicants", None, 25)
    assert page.description is not None
    assert page.description.startswith("Northwind runs search for 40,000 online stores.")
    assert "Build and run Python and Go services on Postgres and Kubernetes" in page.description
    assert "3+ years of backend experience" in page.description
    assert "What you'll do" in page.description  # entities decoded
    assert "Seniority level: Mid-Senior level" in page.description
    assert "Employment type: Full-time" in page.description
    assert "Show more" not in page.description
    assert "Sign in" not in page.description
    assert "<" not in page.description


def test_parses_the_figcaption_form() -> None:
    assert parse_job_page(PAGE_200).applicants == ApplicantCount("Over 200 applicants", 200, None)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("87 applicants", ApplicantCount("87 applicants", 87, 87)),
        ("1 applicant", ApplicantCount("1 applicant", 1, 1)),
        ("Over 100 applicants", ApplicantCount("Over 100 applicants", 100, None)),
    ],
)
def test_parses_every_phrasing_on_a_page(line: str, expected: ApplicantCount) -> None:
    page = PAGE_25.replace("Be among the first 25 applicants", line)
    assert parse_job_page(page).applicants == expected


def test_a_page_without_an_applicant_line() -> None:
    page = PAGE_25.replace("Be among the first 25 applicants", "")
    parsed = parse_job_page(page)
    assert parsed.applicants is None
    assert parsed.description is not None


def test_a_page_that_is_not_a_job_page() -> None:
    assert parse_job_page("<html><body><h1>Sign in</h1></body></html>") == JobPage()


# --- the gate ------------------------------------------------------------------------------


async def test_the_gate_paces_requests() -> None:
    sleep, monotonic = FakeSleep(), FakeMonotonic()
    gate = LinkedInGate(sleep=sleep, rng=random.Random(1), monotonic=monotonic)
    async with gate.turn():
        pass
    assert sleep.calls == []  # nothing before the first request
    async with gate.turn():
        pass
    [pause] = sleep.calls
    assert 4.0 <= pause <= 8.0
    monotonic.now += 60
    async with gate.turn():
        pass
    assert len(sleep.calls) == 1  # long after the last one: no wait


async def test_the_gate_lets_one_request_through_at_a_time() -> None:
    gate = LinkedInGate(sleep=FakeSleep(), monotonic=FakeMonotonic())
    running = most = 0

    async def request() -> None:
        nonlocal running, most
        async with gate.turn():
            running += 1
            most = max(most, running)
            await asyncio.sleep(0)
            running -= 1

    await asyncio.gather(*(request() for _ in range(4)))
    assert most == 1


async def test_a_turn_declined_by_when_neither_pauses_nor_counts() -> None:
    sleep, monotonic = FakeSleep(), FakeMonotonic()
    gate = LinkedInGate(sleep=sleep, rng=random.Random(1), monotonic=monotonic)
    async with gate.turn(when=lambda: True) as allowed:
        assert allowed
    for _ in range(3):
        async with gate.turn(when=lambda: False) as allowed:
            assert not allowed
    assert sleep.calls == []
    monotonic.now += 100  # the declined turns didn't count as requests
    async with gate.turn() as allowed:
        assert allowed
    assert sleep.calls == []


def test_the_gate_cools_down_after_a_block() -> None:
    clock = Clock()
    gate = LinkedInGate(Pacing(cooldown=timedelta(minutes=30)), clock=clock)
    assert gate.blocked_until() is None
    assert gate.back_off() == NOW + timedelta(minutes=30)
    assert gate.blocked_until() == NOW + timedelta(minutes=30)
    clock.now += timedelta(minutes=31)
    assert gate.blocked_until() is None


# --- job pages ----------------------------------------------------------------------------


async def test_enrich_adds_the_description_and_applicants(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    route = respx_mock.get(page_url("4101100003")).respond(200, html=PAGE_25)
    setup = Setup(http)
    enriched = await setup.pages.enrich(linkedin_posting())

    assert route.call_count == 1
    assert "cookie" not in route.calls.last.request.headers
    assert enriched.applicants == ApplicantCount("Be among the first 25 applicants", None, 25)
    assert enriched.description_text is not None
    assert "Postgres" in enriched.description_text
    assert enriched.external_id == "4101100003"


async def test_pages_are_read_one_at_a_time_with_pauses(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    for job_id in ("4101100003", "4101100004", "4101100005"):
        respx_mock.get(page_url(job_id)).respond(200, html=PAGE_25)
    setup = Setup(http)
    await asyncio.gather(
        *(
            setup.pages.enrich(linkedin_posting(job_id))
            for job_id in ("4101100003", "4101100004", "4101100005")
        )
    )
    assert len(setup.sleep.calls) == 2  # between the three pages
    assert all(4.0 <= pause <= 8.0 for pause in setup.sleep.calls)


@pytest.mark.parametrize("status", [429, 999, 403])
async def test_a_block_ends_job_pages_and_starts_the_cooldown(
    respx_mock: respx.MockRouter, http: HttpClient, status: int
) -> None:
    blocked = respx_mock.get(page_url("4101100003")).respond(status)
    later = respx_mock.get(page_url("4101100004")).respond(200, html=PAGE_200)
    setup = Setup(http)

    with pytest.raises(EnrichmentDeferred, match=f"HTTP {status}"):
        await setup.pages.enrich(linkedin_posting("4101100003"))
    assert blocked.call_count == 1  # no retries
    assert setup.gate.blocked_until() == NOW + timedelta(minutes=30)

    second = linkedin_posting("4101100004")
    with pytest.raises(EnrichmentDeferred, match="cool-down until 14:30 UTC"):
        await setup.pages.enrich(second)
    assert later.call_count == 0  # stopped for the rest of the cycle

    setup.clock.now += timedelta(minutes=31)
    assert (await setup.pages.enrich(second)).applicants is not None
    assert later.call_count == 1


async def test_a_redirect_to_the_sign_in_wall_is_a_block(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.get(page_url("4101100003")).respond(
        302, headers={"Location": "https://www.linkedin.com/authwall?trk=bf"}
    )
    respx_mock.get("https://www.linkedin.com/authwall?trk=bf").respond(200, html="<h1>Join</h1>")
    setup = Setup(http)
    with pytest.raises(EnrichmentDeferred, match="sign-in wall"):
        await setup.pages.enrich(linkedin_posting())
    assert setup.gate.blocked_until() is not None


async def test_a_page_that_is_gone_leaves_the_posting_alone(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.get(page_url("4101100003")).respond(404)
    setup = Setup(http)
    posting = linkedin_posting()
    assert await setup.pages.enrich(posting) == posting
    assert setup.gate.blocked_until() is None


@pytest.mark.parametrize("outcome", [httpx.Response(500), httpx.ConnectError("down")])
async def test_a_server_or_network_error_defers_without_a_cooldown(
    respx_mock: respx.MockRouter, http: HttpClient, outcome: Any
) -> None:
    respx_mock.get(page_url("4101100003")).mock(
        side_effect=outcome if isinstance(outcome, Exception) else None,
        return_value=None if isinstance(outcome, Exception) else outcome,
    )
    setup = Setup(http)
    with pytest.raises(EnrichmentDeferred):
        await setup.pages.enrich(linkedin_posting())
    assert setup.gate.blocked_until() is None


async def test_at_most_max_per_hour_pages(respx_mock: respx.MockRouter, http: HttpClient) -> None:
    routes = {
        job_id: respx_mock.get(page_url(job_id)).respond(200, html=PAGE_25)
        for job_id in ("4101100003", "4101100004", "4101100005")
    }
    setup = Setup(http, max_per_hour=2)
    await setup.pages.enrich(linkedin_posting("4101100003"))
    await setup.pages.enrich(linkedin_posting("4101100004"))
    with pytest.raises(EnrichmentDeferred, match="2 job pages read in the last hour"):
        await setup.pages.enrich(linkedin_posting("4101100005"))
    assert [route.call_count for route in routes.values()] == [1, 1, 0]

    setup.monotonic.now += 3601
    enriched = await setup.pages.enrich(linkedin_posting("4101100005"))
    assert enriched.applicants is not None
    assert routes["4101100005"].call_count == 1


async def enrich_while_queued(setup: Setup, job_ids: list[str]) -> list[object]:
    """Enrich every posting while a search holds LinkedIn's turn, so that all of them pass
    the first check and queue behind it, as pages do behind a cycle's searches."""
    async with setup.gate.turn():
        tasks = [
            asyncio.create_task(setup.pages.enrich(linkedin_posting(job_id))) for job_id in job_ids
        ]
        await asyncio.sleep(0)
    return list(await asyncio.gather(*tasks, return_exceptions=True))


async def test_pages_queued_behind_the_cap_defer_without_pausing(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    job_ids = [f"41011000{n:02d}" for n in range(10, 16)]
    for job_id in job_ids:
        respx_mock.get(page_url(job_id)).respond(200, html=PAGE_25)
    setup = Setup(http, max_per_hour=2)
    outcomes = await enrich_while_queued(setup, job_ids)
    assert [isinstance(outcome, EnrichmentDeferred) for outcome in outcomes] == [
        False, False, True, True, True, True,
    ]  # fmt: skip
    assert len(setup.sleep.calls) == 2  # before each of the two pages read


async def test_pages_queued_behind_a_block_defer_without_pausing(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.get(page_url("4101100010")).respond(429)
    others = [f"41011000{n:02d}" for n in range(11, 15)]
    routes = [respx_mock.get(page_url(job_id)).respond(200, html=PAGE_25) for job_id in others]
    setup = Setup(http)
    outcomes = await enrich_while_queued(setup, ["4101100010", *others])
    assert all(isinstance(outcome, EnrichmentDeferred) for outcome in outcomes)
    assert all(route.call_count == 0 for route in routes)
    assert len(setup.sleep.calls) == 1  # before the refused page only


async def test_postings_from_other_sources_are_left_alone(http: HttpClient) -> None:
    setup = Setup(http)
    posting = make_posting()
    with respx.mock(assert_all_called=False) as router:
        assert await setup.pages.enrich(posting) == posting
        assert router.calls.call_count == 0


# --- searches and pages share the gate ---------------------------------------------------


class FakeScrape:
    def __init__(self, *frames: Any) -> None:
        self.frames = list(frames)
        self.calls = 0

    def __call__(self, **arguments: Any) -> Any:
        self.calls += 1
        return self.frames.pop(0)


async def test_a_job_page_429_cools_linkedin_searches_down_too(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.get(page_url("4101100003")).respond(429)
    setup = Setup(http)
    scrape = FakeScrape(jobspy_frame([]))
    searches = JobSpyAdapter(scrape=scrape, gate=setup.gate, clock=setup.clock)

    with pytest.raises(EnrichmentDeferred):
        await setup.pages.enrich(linkedin_posting())
    result = await searches.fetch(SEARCH)
    assert result.skipped
    assert result.error_kind == "http_429"
    assert scrape.calls == 0


async def test_a_search_429_stops_job_pages(respx_mock: respx.MockRouter, http: HttpClient) -> None:
    route = respx_mock.get(page_url("4101100003")).respond(200, html=PAGE_25)
    setup = Setup(http)
    blocked = jobspy_frame(
        [], errors={"linkedin": "response status code 429 (blocked: use a proxy)"}
    )
    searches = JobSpyAdapter(scrape=FakeScrape(blocked), gate=setup.gate, clock=setup.clock)

    assert (await searches.fetch(SEARCH)).error_kind == "http_429"
    with pytest.raises(EnrichmentDeferred, match="cool-down"):
        await setup.pages.enrich(linkedin_posting())
    assert route.call_count == 0


async def test_a_job_page_waits_its_turn_after_a_search(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.get(page_url("4101100003")).respond(200, html=PAGE_25)
    setup = Setup(http)
    searches = JobSpyAdapter(
        scrape=FakeScrape(jobspy_frame([])), gate=setup.gate, clock=setup.clock
    )
    await searches.fetch(SEARCH)
    await setup.pages.enrich(linkedin_posting())
    [pause] = setup.sleep.calls
    assert 4.0 <= pause <= 8.0


async def test_the_linkedin_source_searches_with_jobspy_and_enriches_from_pages(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.get(page_url("4101100003")).respond(200, html=PAGE_25)
    setup = Setup(http)

    class Searches:
        name = "jobspy"

        async def fetch(self, target: Any) -> FetchResult:
            return FetchResult.success([linkedin_posting()], fetched_at=NOW, complete_listing=False)

        async def enrich(self, posting: Posting) -> Posting:
            raise AssertionError("JobSpy has nothing to add")

    source = LinkedInSource(Searches(), setup.pages)
    assert source.name == "linkedin"
    result = await source.fetch(SEARCH)
    assert [p.external_id for p in result.postings] == ["4101100003"]
    assert (await source.enrich(result.postings[0])).applicants is not None
