"""Live smoke test for the SimplifyJobs feed: it parses, and it answers its own ETag with 304.

Skipped unless JOBPULSE_LIVE=1:  JOBPULSE_LIVE=1 uv run pytest -m live -v
"""

import dataclasses

import pytest

from jobpulse.config import SIMPLIFY_NEW_GRAD_URL
from jobpulse.core.models import FeedTarget
from jobpulse.httpclient import HttpClient
from jobpulse.sources.simplify import SimplifyAdapter

pytestmark = [pytest.mark.live, pytest.mark.enable_socket]

FEED = FeedTarget(feed="simplify", name="new-grad", url=SIMPLIFY_NEW_GRAD_URL)


async def test_the_feed_parses_into_well_formed_postings(live_http: HttpClient) -> None:
    result = await SimplifyAdapter(live_http).fetch(FEED)
    assert result.ok, result.error
    assert not result.not_modified
    assert not result.warnings, result.warnings  # every listing parsed
    assert len(result.postings) > 500
    assert len(result.closed_external_ids) > 1000
    assert result.etag
    for posting in result.postings:
        assert posting.source == "simplify"
        assert len(posting.external_id) == 36
        assert posting.title
        assert posting.company_name
        assert posting.url.startswith(("https://", "http://"))
        assert posting.canonical_url
        assert posting.posted_at is not None
        assert posting.posted_at.utcoffset() is not None
    assert {p.posted_at_precision for p in result.postings} <= {"exact", "day"}


async def test_the_stored_etag_gets_a_304(live_http: HttpClient) -> None:
    adapter = SimplifyAdapter(live_http)
    first = await adapter.fetch(FEED)
    assert first.ok, first.error
    again = await adapter.fetch(dataclasses.replace(FEED, etag=first.etag))
    assert again.ok, again.error
    assert again.not_modified, "no 304: the feed changed between the two requests, or ETags broke"
