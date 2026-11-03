"""Item 9 (detection with a page fetch, and validation with one API call)."""

import httpx
import pytest
import respx

from jobpulse.core.atsurls import AtsRef
from jobpulse.httpclient import HttpClient
from jobpulse.sources.ats_detect import AtsDetector, is_fetchable_url
from tests.factories import FIXTURES

ZETA_PAGE = "https://careers.zeta.example/careers/?gh_jid=5550001001"
GH_ACME = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
LEVER_US = "https://api.lever.co/v0/postings/{token}?mode=json"
LEVER_EU = "https://api.eu.lever.co/v0/postings/{token}?mode=json"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"


def page_html() -> str:
    return (FIXTURES / "pages" / "zeta_careers.html").read_text(encoding="utf-8")


async def test_a_board_url_needs_no_request(http: HttpClient) -> None:
    # respx has no routes here, so any request would fail the test.
    with respx.mock(assert_all_mocked=True):
        ref = await AtsDetector(http).detect(
            "https://jobs.lever.co/beta/5c6e2a0e-1f2b-4c3d-9e8f-0a1b2c3d4e5f"
        )
    assert ref == AtsRef("lever", "beta", "5c6e2a0e-1f2b-4c3d-9e8f-0a1b2c3d4e5f")


async def test_otherwise_the_page_is_fetched_once_and_scanned(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    route = respx_mock.get(ZETA_PAGE).respond(200, text=page_html())
    ref = await AtsDetector(http).detect(ZETA_PAGE)
    assert ref == AtsRef("greenhouse", "zeta")
    assert route.call_count == 1
    assert route.calls.last.request.headers["User-Agent"] == "jobpulse-test/0.1"


async def test_no_page_fetch_when_asked_not_to(http: HttpClient) -> None:
    with respx.mock(assert_all_mocked=True):
        assert await AtsDetector(http).detect(ZETA_PAGE, fetch_page=False) is None


@pytest.mark.parametrize(
    "outcome",
    [httpx.Response(404), httpx.Response(500), httpx.ConnectError("refused")],
    ids=["404", "500", "connect-error"],
)
async def test_an_unreadable_page_finds_nothing(
    respx_mock: respx.MockRouter, http: HttpClient, outcome: httpx.Response | Exception
) -> None:
    route = respx_mock.get(ZETA_PAGE)
    if isinstance(outcome, Exception):
        route.mock(side_effect=outcome)
    else:
        route.mock(return_value=outcome)
    detector = AtsDetector(http)
    assert await detector.refs_in_page(ZETA_PAGE) is None
    assert await detector.detect(ZETA_PAGE) is None
    assert route.call_count == 2  # one GET per call, never retried


async def test_a_page_without_boards_finds_an_empty_list(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.get(ZETA_PAGE).respond(200, text="<html><a href='/jobs'>Jobs</a></html>")
    assert await AtsDetector(http).refs_in_page(ZETA_PAGE) == []


async def test_page_reading_stops_at_the_size_cap(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    padded = "<html>" + "x" * 5000 + page_html()
    respx_mock.get(ZETA_PAGE).respond(200, text=padded)
    assert await AtsDetector(http, max_page_bytes=1000).refs_in_page(ZETA_PAGE) == []


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/careers",
        "http://127.0.0.1/jobs",
        "http://192.168.1.10/jobs",
        "http://10.0.0.1/",
        "http://[::1]/careers",
        "http://169.254.169.254/latest/meta-data",
        "http://intranet/jobs",
        "http://printer.local/jobs",
        "file:///etc/passwd",
        "ftp://example.com/jobs",
        "not a url",
    ],
)
async def test_private_and_odd_urls_are_never_fetched(url: str, http: HttpClient) -> None:
    assert not is_fetchable_url(url)
    with respx.mock(assert_all_mocked=True):
        assert await AtsDetector(http).refs_in_page(url) is None


def test_public_urls_are_fetchable() -> None:
    assert is_fetchable_url(ZETA_PAGE)
    assert is_fetchable_url("http://93.184.215.14/careers")


# --- validation: the right API, once -------------------------------------------------------


async def test_greenhouse_validation_calls_the_board_list_once(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    route = respx_mock.get(GH_ACME).respond(
        200,
        json={
            "jobs": [{"id": 1, "title": "Backend Engineer", "company_name": "Acme"}],
            "meta": {"total": 1},
        },
    )
    validation = await AtsDetector(http).validate(AtsRef("greenhouse", "acme", "4012345007"))
    assert route.call_count == 1
    assert len(respx_mock.calls) == 1
    assert validation.ok
    assert validation.ref == AtsRef("greenhouse", "acme")
    assert validation.name == "Acme"
    assert validation.job_count == 1


@pytest.mark.parametrize(
    ("ref", "url", "payload"),
    [
        (AtsRef("lever", "beta"), LEVER_US.format(token="beta"), []),
        (AtsRef("lever", "olx", region="eu"), LEVER_EU.format(token="olx"), [{"id": "x"}]),
        (
            AtsRef("ashby", "Epsilon"),
            ASHBY.format(token="Epsilon"),
            {"apiVersion": "1", "jobs": []},
        ),
    ],
    ids=["lever-us", "lever-eu", "ashby"],
)
async def test_lever_and_ashby_validation_call_their_api_once(
    respx_mock: respx.MockRouter, http: HttpClient, ref: AtsRef, url: str, payload: object
) -> None:
    route = respx_mock.get(url).respond(200, json=payload)
    validation = await AtsDetector(http).validate(ref)
    assert route.call_count == 1
    assert len(respx_mock.calls) == 1
    assert validation.ok
    assert validation.ref == ref
    assert validation.name is None


async def test_lever_us_404_is_retried_once_on_the_eu_host(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    us = respx_mock.get(LEVER_US.format(token="olx")).respond(
        404, json={"ok": False, "error": "Document not found"}
    )
    eu = respx_mock.get(LEVER_EU.format(token="olx")).respond(200, json=[{"id": "a"}, {"id": "b"}])
    validation = await AtsDetector(http).validate(AtsRef("lever", "olx"))
    assert (us.call_count, eu.call_count) == (1, 1)
    assert validation.ok
    assert validation.ref == AtsRef("lever", "olx", region="eu")
    assert validation.job_count == 2


async def test_an_unknown_board_is_a_permanent_miss(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.get(GH_ACME).respond(404, json={"status": 404, "error": "Job not found"})
    validation = await AtsDetector(http).validate(AtsRef("greenhouse", "acme"))
    assert not validation.ok
    assert validation.error_kind == "http_4xx"
    assert validation.permanent


async def test_a_server_error_is_a_transient_miss(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.get(ASHBY.format(token="gamma")).respond(503)
    validation = await AtsDetector(http).validate(AtsRef("ashby", "gamma"))
    assert not validation.ok
    assert validation.error_kind == "http_5xx"
    assert not validation.permanent


async def test_an_unexpected_payload_is_a_permanent_miss(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.get(GH_ACME).respond(200, json={"unexpected": True})
    validation = await AtsDetector(http).validate(AtsRef("greenhouse", "acme"))
    assert not validation.ok
    assert validation.error_kind == "parse"
    assert validation.permanent
