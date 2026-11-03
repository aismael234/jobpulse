"""Detection rule 1 for job boards: ids come from the job URL."""

import pytest

from jobpulse.core.jobboards import board_job_id, indeed_job_key, linkedin_job_id


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/jobs/view/4101234567",
        "https://www.linkedin.com/jobs/view/4101234567/",
        "https://www.linkedin.com/jobs/view/4101234567?refId=abc%3D%3D&trackingId=xyz",
        "https://uk.linkedin.com/jobs/view/backend-engineer-at-acme-4101234567",
        "https://linkedin.com/jobs/view/4101234567?position=1&pageNum=0",
    ],
)
def test_linkedin_job_id(url: str) -> None:
    assert linkedin_job_id(url) == "4101234567"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/jobs/search/?keywords=backend",
        "https://www.linkedin.com/company/acme",
        "https://www.linkedin.com/jobs/view/backend-engineer",
        "https://example.com/jobs/view/4101234567",
        "not a url",
        "",
    ],
)
def test_not_a_linkedin_job(url: str) -> None:
    assert linkedin_job_id(url) is None


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://www.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b", "1a2b3c4d5e6f7a8b"),
        ("https://ca.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b&from=serp", "1a2b3c4d5e6f7a8b"),
        ("https://uk.indeed.com/rc/clk?fccid=x&jk=0f9e8d7c6b5a4321", "0f9e8d7c6b5a4321"),
    ],
)
def test_indeed_job_key(url: str, key: str) -> None:
    assert indeed_job_key(url) == key


@pytest.mark.parametrize(
    "url",
    [
        "https://www.indeed.com/cmp/Acme",
        "https://www.indeed.com/viewjob",
        "https://www.indeed.com/viewjob?jk=",
        "https://example.com/viewjob?jk=1a2b3c4d5e6f7a8b",
        "",
    ],
)
def test_not_an_indeed_job(url: str) -> None:
    assert indeed_job_key(url) is None


def test_board_job_id_picks_the_site_rule() -> None:
    assert board_job_id("linkedin", "https://www.linkedin.com/jobs/view/42424242") == "42424242"
    assert (
        board_job_id("indeed", "https://www.indeed.com/viewjob?jk=abc123def456") == "abc123def456"
    )
    assert board_job_id("indeed", "https://www.linkedin.com/jobs/view/42424242") is None
