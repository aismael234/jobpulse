"""M4 live smoke test: one real Workday board (Workday's own careers site), searched by keyword
and paged without keywords, plus the VERIFY facts behind the adapter: ``limit`` above 20 is a
400, and ``total`` is filled on the first page only.

Skipped unless JOBPULSE_LIVE=1:  JOBPULSE_LIVE=1 uv run pytest -m live -v
"""

import pytest

from jobpulse.core.atsurls import workday_board
from jobpulse.core.models import CompanyTarget
from jobpulse.httpclient import HttpClient
from jobpulse.sources.workday import WorkdayAdapter

pytestmark = [pytest.mark.live, pytest.mark.enable_socket]

TOKEN = "workday.wd5/Workday"


def target(*keywords: str) -> CompanyTarget:
    return CompanyTarget(
        company_id=0, ats="workday", token=TOKEN, name="Workday", keywords=keywords
    )


async def test_a_keyword_search_parses_into_well_formed_postings(live_http: HttpClient) -> None:
    adapter = WorkdayAdapter(live_http)
    result = await adapter.fetch(target("software engineer"))
    assert result.ok, result.error
    assert not result.complete_listing  # keyword results never close anything
    assert result.postings
    for posting in result.postings:
        assert posting.external_id.startswith("workday.wd5/workday/")
        assert posting.url.startswith("https://workday.wd5.myworkdayjobs.com/Workday/job/")
        assert posting.canonical_url.startswith("workday:workday.wd5/workday:")
        assert posting.posted_at is not None
        assert posting.posted_at_precision == "day"

    enriched = await adapter.enrich(result.postings[0])
    assert enriched.description_text
    assert enriched.locations


async def test_paging_without_keywords_stops_at_max_pages(live_http: HttpClient) -> None:
    result = await WorkdayAdapter(live_http, max_pages=2).fetch(target())
    assert result.ok, result.error
    assert len(result.postings) == 40
    assert not result.complete_listing  # the board has more than 40 jobs
    assert result.warnings


async def test_limit_above_20_is_refused_and_total_is_on_the_first_page_only(
    live_http: HttpClient,
) -> None:
    board = workday_board(TOKEN)
    assert board is not None
    url = WorkdayAdapter.jobs_url(board)
    body = WorkdayAdapter.search_body("", 0)
    too_many = await live_http.request("POST", url, json={**body, "limit": 21})
    assert too_many.status_code == 400
    first = await live_http.request("POST", url, json=body)
    second = await live_http.request("POST", url, json={**body, "offset": 20})
    assert first.json()["total"] > 20
    assert second.json()["total"] == 0
    assert len(second.json()["jobPostings"]) == 20
