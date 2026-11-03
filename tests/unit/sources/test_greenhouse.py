import copy
from datetime import UTC, datetime

import httpx
import pytest
import respx

from jobpulse.core.models import CompanyTarget
from jobpulse.httpclient import HttpClient
from jobpulse.sources.greenhouse import GreenhouseAdapter
from tests.factories import NOW, load_fixture

LIST_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
DETAIL_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/4012345007?pay_transparency=true"
TARGET = CompanyTarget(company_id=1, ats="greenhouse", token="acme", name="Acme")


def adapter(http: HttpClient) -> GreenhouseAdapter:
    return GreenhouseAdapter(http, clock=lambda: NOW)


async def test_parses_the_job_list(respx_mock: respx.MockRouter, http: HttpClient) -> None:
    route = respx_mock.get(LIST_URL).respond(200, json=load_fixture("greenhouse/acme_jobs.json"))
    result = await adapter(http).fetch(TARGET)

    assert str(route.calls.last.request.url) == LIST_URL  # no content=true on the list call
    assert result.ok
    assert result.complete_listing
    assert result.fetched_at == NOW
    assert len(result.postings) == 5
    by_id = {p.external_id: p for p in result.postings}

    first = by_id["4012345007"]  # the job post id, not internal_job_id 3537001
    assert first.source == "greenhouse"
    assert first.title == "Backend Engineer, Payments"
    assert first.canonical_url == "greenhouse:acme:4012345007"
    assert first.company_name == "Acme"
    assert first.company_token == "acme"
    # first_published (13:35 UTC), never updated_at (15:02 UTC)
    assert first.posted_at == datetime(2026, 10, 23, 13, 35, tzinfo=UTC)
    assert first.posted_at_precision == "exact"
    assert first.locations == ("San Francisco, CA", "New York, NY")
    assert first.is_remote is False
    assert first.description_text is None
    assert first.raw["requisition_id"] == "ENG-101"

    custom_domain = by_id["4012345008"]
    assert custom_domain.url == "https://careers.acme.example/jobs?gh_jid=4012345008"
    assert custom_domain.locations == ("Remote (US)",)
    assert custom_domain.is_remote is True


async def test_missing_first_published_means_no_timestamp(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    payload = load_fixture("greenhouse/acme_jobs.json")
    del payload["jobs"][0]["first_published"]
    respx_mock.get(LIST_URL).respond(200, json=payload)
    result = await adapter(http).fetch(TARGET)
    first = result.postings[0]
    assert first.posted_at is None
    assert first.posted_at_precision == "none"


async def test_malformed_item_is_skipped_and_blocks_closures(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    payload = load_fixture("greenhouse/acme_jobs.json")
    broken = copy.deepcopy(payload["jobs"][0])
    del broken["id"]
    payload["jobs"].append(broken)
    respx_mock.get(LIST_URL).respond(200, json=payload)
    result = await adapter(http).fetch(TARGET)
    assert result.ok
    assert len(result.postings) == 5
    assert not result.complete_listing
    assert result.warnings


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.Response(500), "http_5xx"),
        (httpx.Response(404, json={"status": 404, "error": "Job not found"}), "http_4xx"),
        (httpx.Response(429, headers={"Retry-After": "1"}), "http_429"),
        (httpx.Response(200, text="<html>not json</html>"), "parse"),
        (httpx.Response(200, json={"unexpected": True}), "parse"),
        (httpx.Response(200, json={"jobs": "nope"}), "parse"),
    ],
    ids=["500", "404", "429", "html", "no-jobs-key", "jobs-not-a-list"],
)
async def test_failures_become_failed_results(
    respx_mock: respx.MockRouter, http: HttpClient, response: httpx.Response, kind: str
) -> None:
    respx_mock.get(LIST_URL).mock(return_value=response)
    result = await adapter(http).fetch(TARGET)
    assert not result.ok
    assert result.error_kind == kind
    assert result.error
    assert result.postings == ()


async def test_timeout_is_a_failed_result(respx_mock: respx.MockRouter, http: HttpClient) -> None:
    respx_mock.get(LIST_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    result = await adapter(http).fetch(TARGET)
    assert not result.ok
    assert result.error_kind == "timeout"


async def test_enrich_adds_description_department_and_pay(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.get(LIST_URL).respond(200, json=load_fixture("greenhouse/acme_jobs.json"))
    respx_mock.get(DETAIL_URL).respond(
        200, json=load_fixture("greenhouse/acme_job_4012345007.json")
    )
    gh = adapter(http)
    posting = (await gh.fetch(TARGET)).postings[0]
    enriched = await gh.enrich(posting)

    assert enriched.description_text is not None
    assert enriched.description_text.startswith("About the role")
    assert "3+ years of professional backend experience" in enriched.description_text
    assert "Kubernetes experience is a plus" in enriched.description_text
    assert "Benefits & perks" in enriched.description_text
    assert "&lt;" not in enriched.description_text
    assert enriched.department == "Engineering"
    assert enriched.compensation is not None
    assert enriched.compensation["pay_input_ranges"][0]["min_cents"] == 18000000
    assert enriched.posted_at == posting.posted_at


@pytest.mark.respx(assert_all_called=True)
async def test_enrich_failure_keeps_the_list_data(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.get(LIST_URL).respond(200, json=load_fixture("greenhouse/acme_jobs.json"))
    respx_mock.get(DETAIL_URL).respond(500)
    gh = adapter(http)
    posting = (await gh.fetch(TARGET)).postings[0]
    assert await gh.enrich(posting) == posting
