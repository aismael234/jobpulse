"""The JobSpy adapter against frames shaped like python-jobspy 1.4.0's, item 8 included."""

import asyncio
import random
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from jobpulse.core.models import CompanyTarget, SearchTarget
from jobpulse.sources.jobspy_boards import (
    JobSpyAdapter,
    Pacing,
    jobspy_error_kind,
    load_scrape_jobs,
)
from tests.factories import (
    INDEED_KEY,
    LINKEDIN_ID,
    NOW,
    FakeMonotonic,
    FakeSleep,
    jobspy_frame,
    jobspy_row,
)

LINKEDIN = SearchTarget(
    search_id=1,
    site="linkedin",
    name="backend-sf",
    search_term="backend engineer",
    location="San Francisco, CA",
    hours_old=3,
    params={"results_wanted": 25, "country_indeed": "USA"},
)
INDEED = SearchTarget(
    search_id=2,
    site="indeed",
    name="backend-remote",
    search_term="backend engineer",
    location="United States",
    hours_old=3,
    params={"results_wanted": 30, "country_indeed": "USA", "is_remote": True},
)
BLOCKED = "response status code 429 (blocked: use a proxy)"


class FakeScrape:
    """``scrape_jobs`` stand-in: records the keyword arguments and returns scripted frames
    (or raises scripted exceptions), in order."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **arguments: Any) -> Any:
        self.calls.append(arguments)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def adapter(
    scrape: Callable[..., Any],
    *,
    clock: Callable[[], datetime] = lambda: NOW,
    sleep: FakeSleep | None = None,
    monotonic: FakeMonotonic | None = None,
    pacing: Pacing | None = None,
) -> JobSpyAdapter:
    return JobSpyAdapter(
        scrape=scrape,
        clock=clock,
        sleep=sleep or FakeSleep(),
        rng=random.Random(7),
        monotonic=monotonic or FakeMonotonic(),
        pacing=pacing or Pacing(),
    )


# --- arguments -----------------------------------------------------------------------


def test_linkedin_arguments() -> None:
    arguments = adapter(FakeScrape()).scrape_arguments(LINKEDIN)
    assert arguments == {
        "site_name": ["linkedin"],
        "search_term": "backend engineer",
        "location": "San Francisco, CA",
        "results_wanted": 25,
        "hours_old": 3,
        "country_indeed": "USA",
        "fetch_description": False,
        "verbose": 0,
    }


def test_indeed_arguments_pass_is_remote_and_never_proxies() -> None:
    arguments = adapter(FakeScrape()).scrape_arguments(INDEED)
    assert arguments["site_name"] == ["indeed"]
    assert arguments["is_remote"] is True
    assert "proxies" not in arguments
    assert "linkedin_fetch_description" not in arguments


def test_linkedin_company_ids_are_passed_when_set() -> None:
    search = SearchTarget(
        search_id=3,
        site="linkedin",
        name="acme-only",
        search_term="engineer",
        params={"linkedin_company_ids": [1035, 2318]},
    )
    arguments = adapter(FakeScrape()).scrape_arguments(search)
    assert arguments["linkedin_company_ids"] == [1035, 2318]
    assert "location" not in arguments


# --- rows ------------------------------------------------------------------------------


async def test_linkedin_rows_become_postings() -> None:
    scrape = FakeScrape(jobspy_frame([jobspy_row("linkedin")]))
    result = await adapter(scrape).fetch(LINKEDIN)

    assert result.ok
    assert not result.complete_listing  # a search window never drives closures
    assert result.fetched_at == NOW
    [posting] = result.postings
    assert posting.source == "linkedin"
    assert posting.external_id == LINKEDIN_ID
    assert posting.company_name == "Acme"
    assert posting.company_token is None
    assert posting.title == "Backend Engineer"
    assert posting.locations == ("San Francisco, CA",)
    assert posting.is_remote is False
    assert posting.url == f"https://www.linkedin.com/jobs/view/{LINKEDIN_ID}"
    assert posting.apply_url is None
    assert posting.canonical_url == f"https://www.linkedin.com/jobs/view/{LINKEDIN_ID}"
    assert posting.posted_at == datetime(2026, 10, 23, tzinfo=UTC)
    assert posting.posted_at_precision == "day"
    assert posting.description_text is None
    assert posting.raw["date_posted"] == "2026-10-23"
    assert posting.raw["company_url"] == "https://www.linkedin.com/company/acme"


async def test_indeed_row_with_a_direct_ats_url() -> None:
    row = jobspy_row(
        "indeed",
        job_url_direct="https://boards.greenhouse.io/acme/jobs/4012345008?gh_src=x1&utm_source=Indeed",
    )
    result = await adapter(FakeScrape(jobspy_frame([row]))).fetch(INDEED)

    [posting] = result.postings
    assert posting.source == "indeed"
    assert posting.external_id == INDEED_KEY
    assert posting.url == f"https://www.indeed.com/viewjob?jk={INDEED_KEY}"
    assert posting.apply_url == row["job_url_direct"]
    assert posting.canonical_url == "greenhouse:acme:4012345008"
    assert posting.description_text is not None
    assert "3+ years of experience" in posting.description_text
    assert "description" not in posting.raw  # kept once, in description_text
    assert posting.compensation == {
        "min_amount": 150000.0,
        "max_amount": 190000.0,
        "currency": "USD",
        "interval": "yearly",
        "salary_source": "direct_data",
    }


async def test_missing_cells_become_none() -> None:
    row = jobspy_row("linkedin", location=None, date_posted=None, is_remote=True)
    other = jobspy_row(external_id="4107777777")
    result = await adapter(FakeScrape(jobspy_frame([row, other]))).fetch(LINKEDIN)
    assert len(result.postings) == 2
    first = next(p for p in result.postings if p.external_id == LINKEDIN_ID)
    assert first.locations == ()
    assert first.is_remote is True
    assert first.posted_at is None
    assert first.posted_at_precision == "none"
    assert first.apply_url is None
    assert first.compensation is None
    assert all(value == value for value in first.raw.values())  # no NaN left in raw


async def test_a_row_without_a_company_keeps_an_empty_name() -> None:
    result = await adapter(FakeScrape(jobspy_frame([jobspy_row("indeed", company=None)]))).fetch(
        INDEED
    )
    [posting] = result.postings
    assert posting.company_name == ""


async def test_the_id_comes_from_job_url_not_the_jobspy_id_column() -> None:
    row = jobspy_row("linkedin", id="li-999")
    result = await adapter(FakeScrape(jobspy_frame([row]))).fetch(LINKEDIN)
    [posting] = result.postings
    assert posting.external_id == LINKEDIN_ID
    assert result.warnings == (
        "1 row(s) whose JobSpy id disagrees with the id in job_url; used the job_url id",
    )


async def test_rows_without_a_usable_job_url_or_title_are_skipped() -> None:
    rows = [
        jobspy_row("linkedin"),
        jobspy_row("linkedin", job_url="https://www.linkedin.com/jobs/search?keywords=x"),
        jobspy_row("linkedin", external_id="4105555555", title=None),
    ]
    result = await adapter(FakeScrape(jobspy_frame(rows))).fetch(LINKEDIN)
    assert [p.external_id for p in result.postings] == [LINKEDIN_ID]
    assert result.warnings == ("skipped 2 row(s) without a usable job_url or title",)


async def test_an_empty_result_is_an_empty_success() -> None:
    result = await adapter(FakeScrape(jobspy_frame([]))).fetch(LINKEDIN)
    assert result.ok
    assert result.postings == ()


# --- errors (item 8) -------------------------------------------------------------------


async def test_a_board_error_is_a_failed_run_even_when_rows_came_back() -> None:
    frame = jobspy_frame([jobspy_row("linkedin")], errors={"linkedin": BLOCKED})
    result = await adapter(FakeScrape(frame)).fetch(LINKEDIN)
    assert not result.ok
    assert result.error_kind == "http_429"
    assert result.error is not None
    assert BLOCKED in result.error
    assert result.postings == ()


async def test_other_board_errors_are_jobspy_board_errors() -> None:
    frame = jobspy_frame([], errors={"linkedin": "unexpected page with no jobs (blocked?)"})
    result = await adapter(FakeScrape(frame)).fetch(LINKEDIN)
    assert not result.ok
    assert result.error_kind == "jobspy_board_error"


async def test_an_error_for_another_board_does_not_fail_this_search() -> None:
    frame = jobspy_frame([jobspy_row("linkedin")], errors={"indeed": BLOCKED})
    result = await adapter(FakeScrape(frame)).fetch(LINKEDIN)
    assert result.ok
    assert len(result.postings) == 1


async def test_board_warnings_reach_the_run_log() -> None:
    frame = jobspy_frame(
        [jobspy_row("linkedin")], warnings={"linkedin": ["is_remote isn't supported, ignoring it"]}
    )
    result = await adapter(FakeScrape(frame)).fetch(LINKEDIN)
    assert result.warnings == ("linkedin: is_remote isn't supported, ignoring it",)


@pytest.mark.parametrize(
    ("exception", "kind"),
    [
        (
            ValueError("Invalid results_wanted: 0. Input should be greater than or equal to 1"),
            "jobspy_board_error",
        ),
        (RuntimeError(BLOCKED), "http_429"),
    ],
)
async def test_exceptions_from_jobspy_become_failed_runs(exception: Exception, kind: str) -> None:
    result = await adapter(FakeScrape(exception)).fetch(LINKEDIN)
    assert not result.ok
    assert result.error_kind == kind
    assert result.error is not None
    assert str(exception) in result.error


async def test_a_result_that_is_not_a_frame_is_a_parse_failure() -> None:
    result = await adapter(FakeScrape(None)).fetch(LINKEDIN)
    assert not result.ok
    assert result.error_kind == "parse"


async def test_a_company_target_is_refused() -> None:
    result = await adapter(FakeScrape()).fetch(
        CompanyTarget(company_id=1, ats="greenhouse", token="acme", name="Acme")
    )
    assert not result.ok
    assert result.error_kind == "other"


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        (BLOCKED, "http_429"),
        ("LinkedIn: response status code 429", "http_429"),
        ("response status code 403 (blocked: use a proxy)", "jobspy_board_error"),
        ("API error: [{'message': 'bad'}]", "jobspy_board_error"),
        (
            "HTTPSConnectionPool(host='apis.indeed.com', port=443): Read timed out.",
            "jobspy_board_error",
        ),
        ("job 4291: redirected", "jobspy_board_error"),
    ],
)
def test_jobspy_error_kind(message: str, kind: str) -> None:
    assert jobspy_error_kind(message) == kind


# --- pacing and back-off -----------------------------------------------------------------


async def test_linkedin_searches_are_paced() -> None:
    sleep = FakeSleep()
    monotonic = FakeMonotonic()
    scrape = FakeScrape(*(jobspy_frame([]) for _ in range(4)))
    boards = adapter(scrape, sleep=sleep, monotonic=monotonic)

    await boards.fetch(LINKEDIN)
    assert sleep.calls == []  # nothing to wait for before the first call
    await boards.fetch(INDEED)
    assert sleep.calls == []  # Indeed isn't paced
    await boards.fetch(LINKEDIN)
    [pause] = sleep.calls
    assert 4.0 <= pause <= 8.0

    monotonic.now += 60  # long after the last LinkedIn call: no wait needed
    await boards.fetch(LINKEDIN)
    assert len(sleep.calls) == 1


async def test_after_a_linkedin_429_linkedin_is_skipped_until_the_cooldown_ends() -> None:
    clock = Clock()
    blocked = jobspy_frame([], errors={"linkedin": BLOCKED})
    scrape = FakeScrape(blocked, jobspy_frame([jobspy_row("indeed")]), jobspy_frame([]))
    boards = adapter(scrape, clock=clock, pacing=Pacing(cooldown=timedelta(minutes=30)))

    first = await boards.fetch(LINKEDIN)
    assert first.error_kind == "http_429"
    assert not first.skipped

    second = await boards.fetch(LINKEDIN)
    assert second.skipped
    assert second.error_kind == "http_429"
    assert len(scrape.calls) == 1  # not attempted

    indeed = await boards.fetch(INDEED)
    assert indeed.ok  # the cool-down is LinkedIn's alone

    clock.now += timedelta(minutes=31)
    third = await boards.fetch(LINKEDIN)
    assert third.ok
    assert len(scrape.calls) == 3


async def test_searches_run_one_at_a_time() -> None:
    lock = threading.Lock()
    running = 0
    most = 0

    def slow_scrape(**arguments: Any) -> Any:
        nonlocal running, most
        with lock:
            running += 1
            most = max(most, running)
        time.sleep(0.02)
        with lock:
            running -= 1
        return jobspy_frame([])

    boards = adapter(slow_scrape)
    results = await asyncio.gather(
        boards.fetch(INDEED), boards.fetch(LINKEDIN), boards.fetch(INDEED)
    )
    assert all(result.ok for result in results)
    assert most == 1


def test_load_scrape_jobs_sets_the_linkedin_page_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobspy
    from jobspy.linkedin import LinkedIn

    monkeypatch.setattr(LinkedIn, "delay", LinkedIn.delay)  # restored after the test
    scrape = load_scrape_jobs(Pacing(delay_min=5.0, delay_max=9.0))
    assert scrape is jobspy.scrape_jobs
    assert LinkedIn.delay == (5.0, 9.0)


def test_rows_parse_with_python_dates_too() -> None:
    frame = jobspy_frame([jobspy_row("linkedin", date_posted=date(2026, 10, 22))])
    result = adapter(FakeScrape()).parse_frame(frame, LINKEDIN, NOW)
    assert result.postings[0].posted_at == datetime(2026, 10, 22, tzinfo=UTC)
