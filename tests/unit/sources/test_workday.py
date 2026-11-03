"""M4: the Workday adapter. Paging never asks for more than 20 jobs, ``total`` comes from the
first page, keyword searches are merged and never complete, and ``postedOn`` is day
precision."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from jobpulse.core.models import CompanyTarget
from jobpulse.httpclient import HttpClient
from jobpulse.sources.workday import PAGE_LIMIT, WorkdayAdapter
from tests.factories import NOW, load_fixture, make_posting

TOKEN = "omega.wd5/External"
JOBS_URL = "https://omega.wd5.myworkdayjobs.com/wday/cxs/omega/External/jobs"
DETAIL_URL = (
    "https://omega.wd5.myworkdayjobs.com/wday/cxs/omega/External"
    "/job/Seattle-WA/Software-Engineer---New-Grad_R0012345"
)
TODAY = datetime(2026, 10, 23, tzinfo=UTC)


def target(keywords: tuple[str, ...] = (), token: str = TOKEN) -> CompanyTarget:
    return CompanyTarget(
        company_id=7, ats="workday", token=token, name="Omega", priority=2, keywords=keywords
    )


def adapter(http: HttpClient, **options: Any) -> WorkdayAdapter:
    return WorkdayAdapter(http, clock=lambda: NOW, **options)


def wd_job(number: int, title: str = "Backend Engineer", **fields: Any) -> dict[str, Any]:
    slug = title.replace(" ", "-").replace(",", "")
    return {
        "title": title,
        "externalPath": f"/job/US-WA-Seattle/{slug}_R{number:07d}",
        "locationsText": "US, WA, Seattle",
        "postedOn": "Posted Today",
        "bulletFields": [f"R{number:07d}"],
        **fields,
    }


class FakeBoard:
    """The CXS jobs endpoint the way Workday answers it: pages of at most 20, ``total`` on the
    first page only, a 400 above 20, and one result list per ``searchText``."""

    def __init__(self, jobs: list[dict[str, Any]], **by_keyword: list[dict[str, Any]]) -> None:
        self.jobs = jobs
        self.by_keyword = by_keyword
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        if body["limit"] > 20:
            return httpx.Response(400, json={"errorCode": "HTTP_400", "httpStatus": 400})
        jobs = self.by_keyword.get(body["searchText"], []) if body["searchText"] else self.jobs
        offset = body["offset"]
        page = jobs[offset : offset + body["limit"]]
        return httpx.Response(
            200,
            json={
                "total": len(jobs) if offset == 0 else 0,
                "jobPostings": page,
                "facets": [],
                "userAuthenticated": False,
            },
        )


async def test_parses_the_job_list(respx_mock: respx.MockRouter, http: HttpClient) -> None:
    route = respx_mock.post(JOBS_URL).respond(200, json=load_fixture("workday/omega_jobs.json"))
    result = await adapter(http).fetch(target())

    assert json.loads(route.calls.last.request.content) == {
        "appliedFacets": {},
        "limit": 20,
        "offset": 0,
        "searchText": "",
    }
    assert result.ok
    assert result.complete_listing  # 3 of total 3, no keywords
    assert result.fetched_at == NOW
    by_title = {p.title: p for p in result.postings}

    payments = by_title["Backend Engineer, Payments"]
    assert payments.source == "workday"
    assert payments.external_id == "omega.wd5/external/backend-engineer--payments_r0012350"
    assert payments.company_name == "Omega"
    assert payments.company_token == TOKEN
    assert payments.url == (
        "https://omega.wd5.myworkdayjobs.com/External/job/US-WA-Seattle/"
        "Backend-Engineer--Payments_R0012350"
    )
    assert payments.apply_url == payments.url
    assert (
        payments.canonical_url == "workday:omega.wd5/external:backend-engineer--payments_r0012350"
    )
    assert payments.posted_at == TODAY  # "Posted Today", day precision
    assert payments.posted_at_precision == "day"
    assert payments.locations == ("Seattle, WA",)  # "US, WA, Seattle" reordered
    assert payments.is_remote is False
    assert payments.workplace_type is None
    assert payments.description_text is None
    assert payments.raw["bulletFields"] == ["R0012350"]

    new_grad = by_title["Software Engineer - New Grad"]
    assert new_grad.posted_at == TODAY - timedelta(days=1)  # "Posted Yesterday"
    assert new_grad.locations == ()  # "2 Locations": the detail call names them
    assert new_grad.workplace_type == "hybrid"  # remoteType "Flex"
    assert new_grad.is_remote is False

    platform = by_title["Platform Engineer"]
    assert platform.posted_at == TODAY - timedelta(days=30)  # "Posted 30+ Days Ago"
    assert platform.locations == ("Remote (US)",)
    assert platform.workplace_type == "remote"
    assert platform.is_remote is True
    assert platform.external_id == "omega.wd5/external/platform-engineer_r0011001-1"


async def test_pages_through_every_job_and_never_asks_for_more_than_20(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    board = FakeBoard([wd_job(n) for n in range(47)])
    respx_mock.post(JOBS_URL).mock(side_effect=board)
    result = await adapter(http, max_pages=100).fetch(target())

    assert [body["offset"] for body in board.bodies] == [0, 20, 40]
    assert all(body["limit"] == PAGE_LIMIT == 20 for body in board.bodies)
    assert result.ok
    assert len(result.postings) == 47
    assert len({p.external_id for p in result.postings}) == 47
    assert result.complete_listing  # every page read, 47 of the first page's 47


async def test_a_listing_that_shrank_while_paging_is_not_complete(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    first = {"total": 45, "jobPostings": [wd_job(n) for n in range(20)]}
    empty = {"total": 0, "jobPostings": []}
    respx_mock.post(JOBS_URL).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=empty)]
    )
    result = await adapter(http).fetch(target())
    assert result.ok
    assert len(result.postings) == 20
    assert not result.complete_listing  # 20 of 45: closures wait for a full read
    assert result.warnings


async def test_max_pages_caps_a_big_board_and_says_so(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    board = FakeBoard([wd_job(n) for n in range(120)])
    respx_mock.post(JOBS_URL).mock(side_effect=board)
    result = await adapter(http, max_pages=3).fetch(target())

    assert len(board.bodies) == 3
    assert all(body["limit"] == 20 for body in board.bodies)
    assert len(result.postings) == 60
    assert not result.complete_listing
    [warning] = result.warnings
    assert "120" in warning
    assert "keywords" in warning


async def test_keywords_are_searched_one_by_one_and_merged(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    shared = wd_job(1, "Backend Platform Engineer")
    board = FakeBoard(
        [wd_job(n, "Other Job") for n in range(500)],
        backend=[shared, *(wd_job(n, "Backend Engineer") for n in range(100, 125))],
        platform=[shared, wd_job(200, "Platform Engineer")],
    )
    respx_mock.post(JOBS_URL).mock(side_effect=board)
    result = await adapter(http).fetch(target(keywords=("backend", "platform")))

    searched = [(body["searchText"], body["offset"]) for body in board.bodies]
    assert searched == [("backend", 0), ("backend", 20), ("platform", 0)]
    assert all(body["limit"] == 20 for body in board.bodies)
    assert result.ok
    assert len(result.postings) == 27  # 26 for backend, 2 for platform, 1 of them shared
    assert not result.complete_listing  # matches only: never closes anything


async def test_an_empty_first_page_of_a_board_with_jobs_is_a_failure(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.post(JOBS_URL).respond(200, json={"total": 12, "jobPostings": []})
    result = await adapter(http).fetch(target())
    assert not result.ok
    assert result.error_kind == "parse"
    assert result.error is not None
    assert "12" in result.error


async def test_an_empty_board_is_an_empty_success(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.post(JOBS_URL).respond(200, json={"total": 0, "jobPostings": []})
    result = await adapter(http).fetch(target())
    assert result.ok
    assert result.postings == ()


@pytest.mark.parametrize(
    ("response", "kind", "hint"),
    [
        (
            httpx.Response(
                404, json={"errorCode": "S21", "message": "not found: Job_Posting_Site_ID=External"}
            ),
            "http_4xx",
            "site",
        ),
        (httpx.Response(422, json={"errorCode": "HTTP_422", "message": ""}), "http_4xx", "wd"),
        (httpx.Response(500, text="oops"), "http_5xx", None),
        (httpx.Response(200, text="<html>maintenance</html>"), "parse", None),
        (httpx.Response(200, json={"total": 3}), "parse", None),
        (httpx.Response(200, json=[1, 2]), "parse", None),
    ],
)
async def test_errors_are_failed_results(
    respx_mock: respx.MockRouter,
    http: HttpClient,
    response: httpx.Response,
    kind: str,
    hint: str | None,
) -> None:
    respx_mock.post(JOBS_URL).mock(return_value=response)
    result = await adapter(http).fetch(target())
    assert not result.ok
    assert result.error_kind == kind
    assert result.postings == ()
    if hint is not None:
        assert result.error is not None
        assert hint in result.error


async def test_a_timeout_is_a_failed_result(respx_mock: respx.MockRouter, http: HttpClient) -> None:
    respx_mock.post(JOBS_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    result = await adapter(http).fetch(target())
    assert not result.ok
    assert result.error_kind == "timeout"


async def test_malformed_items_are_skipped_and_block_closures(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    jobs = [wd_job(1), {"title": "No path"}, "junk", wd_job(2)]
    respx_mock.post(JOBS_URL).respond(200, json={"total": 4, "jobPostings": jobs})
    result = await adapter(http).fetch(target())
    assert result.ok
    assert len(result.postings) == 2
    assert not result.complete_listing
    assert result.warnings


async def test_a_token_that_is_not_a_workday_board_fails(http: HttpClient) -> None:
    result = await adapter(http).fetch(target(token="omega"))
    assert not result.ok
    assert result.error_kind == "other"


# --- the detail call ---------------------------------------------------------------------


def listed_new_grad(http: HttpClient) -> Any:
    """The fixture's "2 Locations" job as the list call parses it."""
    payload = load_fixture("workday/omega_jobs.json")
    [item] = [job for job in payload["jobPostings"] if "New-Grad" in job["externalPath"]]
    posting = adapter(http).parse_job(item, target(), fetched_at=NOW)
    assert posting is not None
    return posting


async def test_enrich_reads_the_job(respx_mock: respx.MockRouter, http: HttpClient) -> None:
    respx_mock.get(DETAIL_URL).respond(200, json=load_fixture("workday/omega_job_new_grad.json"))
    enriched = await adapter(http).enrich(listed_new_grad(http))

    assert enriched.description_text is not None
    assert "Build Python services on Postgres" in enriched.description_text
    assert "<" not in enriched.description_text
    assert enriched.posted_at == datetime(2026, 10, 22, tzinfo=UTC)  # startDate
    assert enriched.posted_at_precision == "day"
    assert enriched.locations == ("Seattle, WA", "San Jose, CA")
    assert enriched.workplace_type == "hybrid"
    assert enriched.is_remote is False
    assert enriched.external_id == listed_new_grad(http).external_id


@pytest.mark.parametrize("status", [404, 500])
async def test_a_failed_detail_call_keeps_the_list_data(
    respx_mock: respx.MockRouter, http: HttpClient, status: int
) -> None:
    respx_mock.get(DETAIL_URL).respond(status, json={"errorCode": "S21"})
    posting = listed_new_grad(http)
    assert await adapter(http).enrich(posting) == posting


async def test_enrich_leaves_other_sources_alone(http: HttpClient) -> None:
    posting = make_posting()
    with respx.mock(assert_all_called=False) as router:
        assert await adapter(http).enrich(posting) == posting
        assert router.calls.call_count == 0
