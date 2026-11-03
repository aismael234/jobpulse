import copy
import dataclasses
from datetime import UTC, datetime

import httpx
import pytest
import respx

from jobpulse.config import SIMPLIFY_NEW_GRAD_URL
from jobpulse.core.models import FeedTarget, SearchTarget
from jobpulse.httpclient import HttpClient
from jobpulse.sources.simplify import SimplifyAdapter, normalize_category
from tests.factories import NOW, load_fixture

TARGET = FeedTarget(feed="simplify", name="new-grad", url=SIMPLIFY_NEW_GRAD_URL)
ETAG = 'W/"c3956a3dc66b07cfb77baebebe84523986cb440c0fae8e246f96ff7489fd33cf"'
OPEN_IDS = {
    "9253f8f7-1c19-4dc7-9d83-80b7c9a001ae",  # Acme, Greenhouse
    "06881a69-3549-4e6e-a479-99b3182d6b75",  # Delta, Lever /apply
    "699da123-9ddf-4097-9e15-d30ece137eda",  # Epsilon, Ashby /application
    "f501acdb-9d58-4fc1-94cf-bf552c2e0d46",  # Zeta, gh_jid on a custom domain
    "b5b4b72d-f134-47ef-bdc8-5e766e013555",  # Omega, Workday
    "89b67433-eb60-40a0-bec2-560c37897912",  # Kappa, hardware
    "09adeef9-8c13-4cd8-8f77-89eb9390f5a9",  # Lumen Labs, long-form category
}
CLOSED_IDS = {
    "6489d501-71ee-4406-adf9-db96db821d6e",  # active: false
    "f69bb26e-3222-47c3-a19d-d8c82bffff6b",  # active: false and is_visible: false
}


def adapter(http: HttpClient) -> SimplifyAdapter:
    return SimplifyAdapter(http, clock=lambda: NOW)


async def test_parses_open_listings_and_reports_closed_ones(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    route = respx_mock.get(SIMPLIFY_NEW_GRAD_URL).respond(
        200, json=load_fixture("simplify/listings.json"), headers={"ETag": ETAG}
    )
    result = await adapter(http).fetch(TARGET)

    assert "If-None-Match" not in route.calls.last.request.headers
    assert result.ok
    assert not result.complete_listing  # closures come from active: false, never absence
    assert result.fetched_at == NOW
    assert result.etag == ETAG
    assert {p.external_id for p in result.postings} == OPEN_IDS
    assert set(result.closed_external_ids) == CLOSED_IDS
    by_company = {p.company_name: p for p in result.postings}

    acme = by_company["Acme"]
    assert acme.source == "simplify"
    assert acme.company_token is None
    assert acme.title == "Backend Engineer, Payments"
    assert acme.url == "https://job-boards.greenhouse.io/acme/jobs/4012345007"
    assert acme.apply_url == acme.url
    assert acme.canonical_url == "greenhouse:acme:4012345007"
    assert acme.posted_at == datetime(2026, 10, 23, 13, 52, tzinfo=UTC)
    assert acme.posted_at_precision == "exact"
    assert acme.locations == ("San Francisco, CA", "New York, NY")
    assert acme.is_remote is False
    assert acme.department == "Software"
    assert acme.raw["degrees"] == ["Bachelor's"]

    delta = by_company["Delta"]
    assert delta.canonical_url == "lever:delta:98959f08-1d1b-4b75-919e-833f825394b0"
    assert delta.posted_at == datetime(2026, 10, 23, tzinfo=UTC)
    assert delta.posted_at_precision == "day"  # exactly midnight UTC
    assert delta.department == "Software"  # "Software Engineering" folded

    epsilon = by_company["Epsilon"]
    assert epsilon.canonical_url == "ashby:epsilon:13f15726-9a2d-4f02-a371-2d4d599ff556"
    assert epsilon.locations == ("Remote (US)",)
    assert epsilon.is_remote is True

    zeta = by_company["Zeta"]
    assert zeta.canonical_url == "https://careers.zeta.example/careers?gh_jid=5550001001"

    assert by_company["Lumen Labs"].department == "AI/ML/Data"


async def test_sends_the_stored_etag_and_a_304_is_an_unchanged_success(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    route = respx_mock.get(SIMPLIFY_NEW_GRAD_URL).respond(304)
    result = await adapter(http).fetch(dataclasses.replace(TARGET, etag=ETAG))

    assert route.calls.last.request.headers["If-None-Match"] == ETAG
    assert result.ok
    assert result.not_modified
    assert result.postings == ()
    assert result.closed_external_ids == ()
    assert result.etag == ETAG


async def test_a_304_without_a_stored_etag_is_a_failure(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.get(SIMPLIFY_NEW_GRAD_URL).respond(304)
    result = await adapter(http).fetch(TARGET)
    assert not result.ok
    assert result.error_kind == "other"


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.Response(500), "http_5xx"),
        (httpx.Response(404, text="404: Not Found"), "http_4xx"),
        (httpx.Response(429, headers={"Retry-After": "1"}), "http_429"),
        (httpx.Response(200, text="<html>rate limited</html>"), "parse"),
        (httpx.Response(200, json={"listings": []}), "parse"),
    ],
    ids=["500", "404", "429", "html", "not-a-list"],
)
async def test_failures_become_failed_results(
    respx_mock: respx.MockRouter, http: HttpClient, response: httpx.Response, kind: str
) -> None:
    respx_mock.get(SIMPLIFY_NEW_GRAD_URL).mock(return_value=response)
    result = await adapter(http).fetch(TARGET)
    assert not result.ok
    assert result.error_kind == kind
    assert result.postings == ()
    assert result.closed_external_ids == ()


async def test_timeout_is_a_failed_result(respx_mock: respx.MockRouter, http: HttpClient) -> None:
    respx_mock.get(SIMPLIFY_NEW_GRAD_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    result = await adapter(http).fetch(TARGET)
    assert not result.ok
    assert result.error_kind == "timeout"


async def test_malformed_listings_are_skipped_with_a_warning(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    listings = load_fixture("simplify/listings.json")
    no_id = copy.deepcopy(listings[0])
    no_id["id"] = None
    no_title = copy.deepcopy(listings[1])
    no_title["id"] = "00000000-0000-4000-8000-000000000001"
    no_title["title"] = ""
    listings += [no_id, no_title, "garbage"]
    respx_mock.get(SIMPLIFY_NEW_GRAD_URL).respond(200, json=listings)

    result = await adapter(http).fetch(TARGET)
    assert result.ok
    assert {p.external_id for p in result.postings} == OPEN_IDS
    assert result.warnings == ("skipped 3 malformed listing(s)",)


async def test_a_listing_without_a_usable_date_has_no_timestamp(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    listings = load_fixture("simplify/listings.json")[:1]
    listings[0]["date_posted"] = None
    respx_mock.get(SIMPLIFY_NEW_GRAD_URL).respond(200, json=listings)
    [posting] = (await adapter(http).fetch(TARGET)).postings
    assert posting.posted_at is None
    assert posting.posted_at_precision == "none"


async def test_a_search_target_is_refused(http: HttpClient) -> None:
    search = SearchTarget(search_id=1, site="linkedin", name="s", search_term="backend")
    result = await adapter(http).fetch(search)
    assert not result.ok
    assert result.error_kind == "other"


@pytest.mark.parametrize(
    ("value", "category"),
    [
        ("Software", "Software"),
        ("Software Engineering", "Software"),
        ("Data Science, AI & Machine Learning", "AI/ML/Data"),
        ("Quant", "Quant"),
        ("  Hardware ", "Hardware"),
        ("", None),
        (None, None),
        (5, None),
    ],
)
def test_normalize_category(value: object, category: str | None) -> None:
    assert normalize_category(value) == category
