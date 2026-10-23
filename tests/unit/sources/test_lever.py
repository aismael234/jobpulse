from datetime import UTC, datetime

import httpx
import respx

from jobpulse.core.models import CompanyTarget
from jobpulse.httpclient import HttpClient
from jobpulse.sources.lever import LeverAdapter
from tests.factories import NOW, load_fixture

US_URL = "https://api.lever.co/v0/postings/beta?mode=json"
EU_URL = "https://api.eu.lever.co/v0/postings/beta?mode=json"
TARGET = CompanyTarget(company_id=2, ats="lever", token="beta", name="Beta")
FIRST_ID = "5c6e2a0e-1f2b-4c3d-9e8f-0a1b2c3d4e5f"


def adapter(http: HttpClient) -> LeverAdapter:
    return LeverAdapter(http, clock=lambda: NOW)


async def test_parses_postings(respx_mock: respx.MockRouter, http: HttpClient) -> None:
    respx_mock.get(US_URL).respond(200, json=load_fixture("lever/beta_postings.json"))
    result = await adapter(http).fetch(TARGET)

    assert result.ok
    assert result.complete_listing
    by_id = {p.external_id: p for p in result.postings}
    assert len(by_id) == 3

    first = by_id[FIRST_ID]
    assert first.title == "Senior Backend Engineer, API Platform"
    assert first.canonical_url == f"lever:beta:{FIRST_ID}"
    assert first.posted_at == datetime(2026, 10, 23, 10, 0, tzinfo=UTC)
    assert first.posted_at_precision == "exact"
    assert first.locations == ("San Francisco, CA", "Remote (US)")
    assert first.workplace_type == "hybrid"
    assert first.is_remote is False
    assert first.url == f"https://jobs.lever.co/beta/{FIRST_ID}"
    assert first.apply_url == f"https://jobs.lever.co/beta/{FIRST_ID}/apply"
    assert first.department == "Engineering"
    assert first.compensation == {
        "currency": "USD",
        "interval": "per-year-salary",
        "min": 180000,
        "max": 230000,
    }
    assert first.description_text is not None
    assert "own our public API platform" in first.description_text
    assert "5+ years of experience building backend services" in first.description_text
    assert "PostgreSQL at scale" in first.description_text
    assert "equal opportunity employer" in first.description_text

    remote = by_id["9f8e7d6c-5b4a-4321-8fed-cba987654321"]
    assert remote.workplace_type == "remote"
    assert remote.is_remote is True
    assert remote.department == "Infrastructure"
    assert remote.compensation is None


async def test_eu_region_uses_the_eu_host(respx_mock: respx.MockRouter, http: HttpClient) -> None:
    route = respx_mock.get(EU_URL).respond(200, json=[])
    eu_target = CompanyTarget(company_id=2, ats="lever", token="beta", name="Beta", region="eu")
    result = await adapter(http).fetch(eu_target)
    assert result.ok
    assert route.called


async def test_404_suggests_the_eu_region(respx_mock: respx.MockRouter, http: HttpClient) -> None:
    respx_mock.get(US_URL).respond(404, json={"ok": False, "error": "Document not found"})
    result = await adapter(http).fetch(TARGET)
    assert not result.ok
    assert result.error_kind == "http_4xx"
    assert result.error is not None
    assert "region: eu" in result.error


async def test_non_array_payload_is_a_parse_failure(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.get(US_URL).respond(200, json={"postings": []})
    result = await adapter(http).fetch(TARGET)
    assert not result.ok
    assert result.error_kind == "parse"


async def test_unspecified_workplace_type_falls_back_to_locations(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    payload = load_fixture("lever/beta_postings.json")[:1]
    payload[0]["workplaceType"] = "unspecified"
    payload[0]["categories"]["allLocations"] = ["Remote"]
    respx_mock.get(US_URL).respond(200, json=payload)
    posting = (await adapter(http).fetch(TARGET)).postings[0]
    assert posting.workplace_type is None
    assert posting.is_remote is True


async def test_server_error_is_a_failed_result(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.get(US_URL).mock(return_value=httpx.Response(502))
    result = await adapter(http).fetch(TARGET)
    assert result.error_kind == "http_5xx"
