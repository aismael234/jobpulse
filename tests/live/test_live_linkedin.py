"""M4 live smoke test: one LinkedIn search through JobSpy (one result), then that job's public
page, paced through the shared gate like the worker does it.

Skipped unless JOBPULSE_LIVE=1:  JOBPULSE_LIVE=1 uv run pytest -m live -v
"""

import pytest

from jobpulse.core.models import SearchTarget
from jobpulse.httpclient import HttpClient
from jobpulse.sources.jobspy_boards import JobSpyAdapter
from jobpulse.sources.linkedin_gate import LinkedInGate
from jobpulse.sources.linkedin_guest import LinkedInJobPages

pytestmark = [pytest.mark.live, pytest.mark.enable_socket]

SEARCH = SearchTarget(
    search_id=0,
    site="linkedin",
    name="live",
    search_term="backend engineer",
    location="United States",
    hours_old=72,
    params={"results_wanted": 1},
)


async def test_a_job_page_gives_the_description_and_the_applicant_line(
    live_http: HttpClient,
) -> None:
    gate = LinkedInGate()
    result = await JobSpyAdapter(gate=gate).fetch(SEARCH)
    assert result.ok, result.error
    assert result.postings
    posting = result.postings[0]

    enriched = await LinkedInJobPages(live_http, gate).enrich(posting)
    assert gate.blocked_until() is None, "LinkedIn refused the job page"
    assert enriched.description_text
    assert enriched.applicants is not None, "no applicant line on the page"
    assert enriched.applicants.label.endswith(("applicant", "applicants"))
