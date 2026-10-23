from datetime import UTC, datetime

import respx

from jobpulse.core.models import CompanyTarget
from jobpulse.httpclient import HttpClient
from jobpulse.sources.ashby import AshbyAdapter
from tests.factories import NOW, load_fixture

URL = "https://api.ashbyhq.com/posting-api/job-board/gamma?includeCompensation=true"
TARGET = CompanyTarget(company_id=3, ats="ashby", token="gamma", name="Gamma")
FIRST_ID = "d3bc1ced-3ce4-4086-a050-555055dbb1ff"
NO_ID_UUID = "7a1d9c4e-2b3f-4e5a-8c6d-1e2f3a4b5c6d"


def adapter(http: HttpClient) -> AshbyAdapter:
    return AshbyAdapter(http, clock=lambda: NOW)


async def test_parses_listed_jobs(respx_mock: respx.MockRouter, http: HttpClient) -> None:
    respx_mock.get(URL).respond(200, json=load_fixture("ashby/gamma_job_board.json"))
    result = await adapter(http).fetch(TARGET)

    assert result.ok
    assert result.complete_listing  # skipping an unlisted job isn't a parse problem
    by_id = {p.external_id: p for p in result.postings}
    assert set(by_id) == {FIRST_ID, NO_ID_UUID, "66666666-7777-4888-8999-aaaaaaaaaaaa"}

    first = by_id[FIRST_ID]
    assert first.title == "Backend Engineer"
    assert first.canonical_url == f"ashby:gamma:{FIRST_ID}"
    assert first.posted_at == datetime(2026, 10, 23, 9, 15, 0, 158000, tzinfo=UTC)
    assert first.locations == ("New York, NY", "San Francisco, CA")
    assert first.workplace_type == "hybrid"
    assert first.is_remote is False  # Ashby sets isRemote on Hybrid jobs too
    assert first.department == "Engineering"
    assert first.url == f"https://jobs.ashbyhq.com/gamma/{FIRST_ID}"
    assert first.apply_url == f"https://jobs.ashbyhq.com/gamma/{FIRST_ID}/application"
    assert first.description_text is not None
    assert "2+ years of experience with Python or Go" in first.description_text
    assert first.compensation is not None
    assert first.compensation["scrapeableCompensationSalarySummary"] == "$170K - $220K"

    no_id = by_id[NO_ID_UUID]  # id missing: the UUID at the end of jobUrl
    assert no_id.canonical_url == f"ashby:gamma:{NO_ID_UUID}"
    assert no_id.workplace_type == "remote"
    assert no_id.is_remote is True
    assert no_id.locations == ("Remote (US)",)
    assert no_id.compensation is None  # all-empty compensation block

    unknown = by_id["66666666-7777-4888-8999-aaaaaaaaaaaa"]
    assert unknown.workplace_type is None
    assert unknown.is_remote is None


async def test_job_without_id_or_job_url_uuid_is_malformed(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    payload = load_fixture("ashby/gamma_job_board.json")
    payload["jobs"][1]["jobUrl"] = "https://jobs.ashbyhq.com/gamma"
    respx_mock.get(URL).respond(200, json=payload)
    result = await adapter(http).fetch(TARGET)
    assert result.ok
    assert len(result.postings) == 2
    assert not result.complete_listing


async def test_unknown_board_is_a_failed_result(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.get(URL).respond(404, text="Not Found")
    result = await adapter(http).fetch(TARGET)
    assert not result.ok
    assert result.error_kind == "http_4xx"


async def test_missing_jobs_list_is_a_parse_failure(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.get(URL).respond(200, json={"apiVersion": "1"})
    result = await adapter(http).fetch(TARGET)
    assert result.error_kind == "parse"
