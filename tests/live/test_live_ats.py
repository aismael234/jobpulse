"""Item 17: one small real board per ATS, plus the spec's VERIFY probes.

Skipped unless JOBPULSE_LIVE=1:  JOBPULSE_LIVE=1 uv run pytest -m live -v
The User-Agent carries the contact_email from ./config.yaml when it exists.
"""

import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jobpulse.config import load_config
from jobpulse.core.models import CompanyTarget
from jobpulse.httpclient import REPLAY_HEADER, HttpClient, build_user_agent
from jobpulse.sources.ashby import AshbyAdapter
from jobpulse.sources.base import HttpAdapter
from jobpulse.sources.greenhouse import GreenhouseAdapter
from jobpulse.sources.lever import LeverAdapter

pytestmark = [pytest.mark.live, pytest.mark.enable_socket]

CONFIG = Path(__file__).resolve().parents[2] / "config.yaml"
ADAPTERS: dict[str, type[HttpAdapter]] = {
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "ashby": AshbyAdapter,
}


def contact_email() -> str:
    if CONFIG.exists():
        return load_config(CONFIG).contact_email
    return f"jobpulse-dev-{secrets.token_hex(4)}@example.com"


@pytest.fixture
async def live_http() -> AsyncIterator[HttpClient]:
    async with HttpClient(user_agent=build_user_agent(contact_email())) as client:
        yield client


def target(ats: str, token: str, region: str = "us") -> CompanyTarget:
    return CompanyTarget(company_id=0, ats=ats, token=token, name=token.title(), region=region)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("ats", "token"), [("greenhouse", "discord"), ("lever", "spotify"), ("ashby", "posthog")]
)
async def test_a_real_board_parses_into_well_formed_postings(
    live_http: HttpClient, ats: str, token: str
) -> None:
    result = await ADAPTERS[ats](live_http).fetch(target(ats, token))
    assert result.ok, result.error
    assert result.complete_listing, result.warnings
    assert result.postings
    for posting in result.postings:
        assert posting.external_id
        assert posting.title
        assert posting.url.startswith("https://")
        assert posting.canonical_url == f"{ats}:{token}:{posting.external_id.lower()}"
        assert posting.posted_at is not None
        assert posting.posted_at.utcoffset() is not None
        assert posting.posted_at_precision == "exact"


async def test_greenhouse_list_has_first_published_and_detail_has_content(
    live_http: HttpClient,
) -> None:
    adapter = GreenhouseAdapter(live_http)
    result = await adapter.fetch(target("greenhouse", "discord"))
    assert all(posting.posted_at is not None for posting in result.postings)
    enriched = await adapter.enrich(result.postings[0])
    assert enriched.description_text
    assert "&lt;" not in enriched.description_text


async def test_greenhouse_eu_board_is_served_by_the_global_api(live_http: HttpClient) -> None:
    result = await GreenhouseAdapter(live_http).fetch(target("greenhouse", "proton"))
    assert result.ok, result.error
    assert result.postings


async def test_lever_eu_board_answers_only_on_the_eu_host(live_http: HttpClient) -> None:
    us = await LeverAdapter(live_http).fetch(target("lever", "olx", region="us"))
    assert not us.ok
    assert us.error_kind == "http_4xx"
    eu = await LeverAdapter(live_http).fetch(target("lever", "olx", region="eu"))
    assert eu.ok, eu.error
    assert eu.postings


@pytest.mark.parametrize(
    "url",
    [
        GreenhouseAdapter.list_url("discord"),
        LeverAdapter.list_url("spotify"),
        AshbyAdapter.list_url("posthog"),
    ],
    ids=["greenhouse", "lever", "ashby"],
)
async def test_list_endpoints_answer_if_none_match_with_304(
    live_http: HttpClient, url: str
) -> None:
    first = await live_http.request("GET", url, use_etag_cache=True)
    second = await live_http.request("GET", url, use_etag_cache=True)
    assert first.status_code == 200
    assert second.headers.get(REPLAY_HEADER) == "1", "no 304: the ETag cache was not used"
