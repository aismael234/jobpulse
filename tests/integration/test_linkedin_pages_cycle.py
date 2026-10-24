"""M4: LinkedIn job pages through the whole cycle. A new LinkedIn match gets its page read once
and its applicant count scored; baseline, filtered and merged postings never cost a page; a
429 ends job pages for the cycle and cools the next LinkedIn search down."""

import random
import re
from dataclasses import dataclass, field

import httpx
import pytest
import respx

from jobpulse.sources.jobspy_boards import JobSpyAdapter
from jobpulse.sources.linkedin_gate import LinkedInGate, Pacing
from jobpulse.sources.linkedin_guest import JOB_PAGE_URL, LinkedInJobPages, LinkedInSource
from tests.factories import FIXTURES, NOW, FakeMonotonic, FakeSleep, jobspy_frame, jobspy_row
from tests.integration.conftest import Env, GreenhouseMock, ScriptedScrape, gh_job

PAGE_25 = (FIXTURES / "linkedin" / "job_4101100003.html").read_text(encoding="utf-8")
ANY_PAGE = re.compile(re.escape(JOB_PAGE_URL.format(job_id="")) + r"\d+")
BLOCKED = "response status code 429 (blocked: use a proxy)"


def linkedin(job_id: str, title: str = "Backend Engineer", **fields: object) -> dict[str, object]:
    return jobspy_row("linkedin", external_id=job_id, title=title, **fields)


@dataclass
class LinkedIn:
    """The ``linkedin`` source's moving parts: the scripted search, and job pages answered
    from ``pages`` (by job id; 404 otherwise), with every requested id kept."""

    scrape: ScriptedScrape
    sleep: FakeSleep
    pages: dict[str, httpx.Response] = field(default_factory=dict)
    requested: list[str] = field(default_factory=list)

    def answer(self, request: httpx.Request) -> httpx.Response:
        job_id = request.url.path.rsplit("/", 1)[-1]
        self.requested.append(job_id)
        return self.pages.get(job_id, httpx.Response(404))


@pytest.fixture
def li(env: Env, respx_mock: respx.MockRouter) -> LinkedIn:
    """The ``linkedin`` source as runtime wires it: JobSpy searches and job pages behind one
    gate (no real sleeping)."""
    scrape, sleep = ScriptedScrape(), FakeSleep()
    gate = LinkedInGate(Pacing(), sleep=sleep, rng=random.Random(5), monotonic=FakeMonotonic())
    searches = JobSpyAdapter(scrape=scrape, gate=gate)
    env.adapters["linkedin"] = LinkedInSource(searches, LinkedInJobPages(env.http, gate))
    env.add_search("backend-sf", "linkedin", location="San Francisco, CA")
    setup = LinkedIn(scrape, sleep)
    respx_mock.get(url__regex=ANY_PAGE.pattern).mock(side_effect=setup.answer)
    return setup


async def baseline(env: Env, li: LinkedIn) -> None:
    li.scrape.queue("linkedin", jobspy_frame([linkedin("4101000001")]))
    await env.poll(sources=["linkedin"])


async def test_a_new_match_gets_its_page_read_and_its_applicants_scored(
    env: Env, li: LinkedIn
) -> None:
    await baseline(env, li)
    assert li.requested == []  # a baseline never reads pages

    li.pages["4101100003"] = httpx.Response(200, html=PAGE_25)
    li.scrape.queue(
        "linkedin",
        jobspy_frame(
            [
                linkedin("4101000001"),
                linkedin(
                    "4101100003",
                    "Backend Engineer, Search",
                    company="Northwind",
                    location="Seattle, WA, USA",
                ),
            ]
        ),
    )
    await env.poll(sources=["linkedin"])

    assert li.requested == ["4101100003"]
    row = env.posting("linkedin", "4101100003")
    assert (row.applicant_label, row.applicant_min, row.applicant_max) == (
        "Be among the first 25 applicants",
        None,
        25,
    )
    assert row.description_text is not None
    assert "Seniority level: Mid-Senior level" in row.description_text
    assert row.score_breakdown is not None
    assert row.score_breakdown["applicants"] == pytest.approx(0.1 * 0.875)
    assert row.score_breakdown["stack"] == pytest.approx(0.125)  # "python" in the page, of 2
    [push] = env.notifier.sent
    assert push.title.startswith("Northwind · Backend Engineer, Search · Seattle")


async def test_filtered_and_merged_postings_never_cost_a_page(
    env: Env, li: LinkedIn, gh: GreenhouseMock
) -> None:
    gh.respond(gh_job(4012345007, "Backend Engineer, Payments", published=NOW))
    await env.poll(sources=["greenhouse"])
    await baseline(env, li)

    li.scrape.queue(
        "linkedin",
        jobspy_frame(
            [
                linkedin("4101100010", "Engineering Manager, Backend"),  # title exclude
                linkedin(
                    "4101100011", "Backend Engineer, Payments", location="San Francisco, CA, USA"
                ),
            ]
        ),
    )
    report = await env.poll(sources=["linkedin"])

    [target] = report.targets
    assert target.merged == 1  # the Acme copy, by fingerprint
    assert target.filtered == 1
    assert li.requested == []
    assert env.posting("linkedin", "4101100011").merged_into_id is not None


async def test_a_429_ends_job_pages_and_cools_the_next_search_down(env: Env, li: LinkedIn) -> None:
    await baseline(env, li)
    li.pages["4101100003"] = httpx.Response(429)
    li.pages["4101100004"] = httpx.Response(200, html=PAGE_25)
    li.scrape.queue(
        "linkedin",
        jobspy_frame(
            [
                linkedin("4101100003", "Backend Engineer, Search"),
                linkedin("4101100004", "Backend Engineer, Data"),
            ]
        ),
    )
    report = await env.poll(sources=["linkedin"])

    assert li.requested == ["4101100003"]  # the second page waits for another cycle
    assert {target.ok for target in report.targets} == {True}  # the search itself worked
    for job_id in ("4101100003", "4101100004"):
        row = env.posting("linkedin", job_id)
        assert row.tier in ("instant", "digest")  # judged without the page
        assert row.applicant_label is None

    calls = len(li.scrape.calls)
    cooled = await env.poll(sources=["linkedin"])
    [target] = cooled.targets
    assert target.skipped
    assert target.error_kind == "http_429"
    assert len(li.scrape.calls) == calls  # the search wasn't attempted


async def test_a_search_429_stops_pages_too(env: Env, li: LinkedIn) -> None:
    await baseline(env, li)
    li.scrape.queue("linkedin", jobspy_frame([], errors={"linkedin": BLOCKED}))
    await env.poll(sources=["linkedin"])
    assert li.requested == []
