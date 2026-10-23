"""Item 9 (ATS forms): every Greenhouse, Lever and Ashby URL form is recognized."""

import pytest

from jobpulse.core.atsurls import AtsRef, detect_ats_url, find_ats_refs_in_html, gh_jid

LEVER_ID = "5c6e2a0e-1f2b-4c3d-9e8f-0a1b2c3d4e5f"
ASHBY_ID = "d3bc1ced-3ce4-4086-a050-555055dbb1ff"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://job-boards.greenhouse.io/acme", AtsRef("greenhouse", "acme")),
        ("https://boards.greenhouse.io/acme", AtsRef("greenhouse", "acme")),
        (
            "https://boards.greenhouse.io/acme/jobs/4012345007",
            AtsRef("greenhouse", "acme", "4012345007"),
        ),
        (
            "https://job-boards.greenhouse.io/acme/jobs/4012345007?gh_jid=4012345007#app",
            AtsRef("greenhouse", "acme", "4012345007"),
        ),
        ("https://boards.greenhouse.io/embed/job_board?for=acme", AtsRef("greenhouse", "acme")),
        (
            "https://job-boards.greenhouse.io/embed/job_board?for=acme&b=https%3A%2F%2Facme.example",
            AtsRef("greenhouse", "acme"),
        ),
        ("https://boards.greenhouse.io/embed/job_board/js?for=acme", AtsRef("greenhouse", "acme")),
        (
            "https://boards.greenhouse.io/embed/job_app?for=acme&token=4012345007",
            AtsRef("greenhouse", "acme", "4012345007"),
        ),
        (
            "https://job-boards.eu.greenhouse.io/acme/jobs/55",
            AtsRef("greenhouse", "acme", "55", region="eu"),
        ),
        ("https://boards-api.greenhouse.io/v1/boards/acme/jobs", AtsRef("greenhouse", "acme")),
        ("https://jobs.lever.co/beta", AtsRef("lever", "beta")),
        (f"https://jobs.lever.co/beta/{LEVER_ID}", AtsRef("lever", "beta", LEVER_ID)),
        (
            f"https://jobs.lever.co/beta/{LEVER_ID.upper()}/apply?lever-source=LinkedIn",
            AtsRef("lever", "beta", LEVER_ID),
        ),
        ("https://jobs.eu.lever.co/olx", AtsRef("lever", "olx", region="eu")),
        ("https://jobs.ashbyhq.com/gamma", AtsRef("ashby", "gamma")),
        (f"https://jobs.ashbyhq.com/gamma/{ASHBY_ID}", AtsRef("ashby", "gamma", ASHBY_ID)),
        (
            f"https://jobs.ashbyhq.com/gamma/{ASHBY_ID}/application?embed=true",
            AtsRef("ashby", "gamma", ASHBY_ID),
        ),
        ("jobs.lever.co/beta", AtsRef("lever", "beta")),
        ("HTTPS://JOBS.LEVER.CO/Beta", AtsRef("lever", "beta")),
    ],
)
def test_detects_every_ats_form(url: str, expected: AtsRef) -> None:
    assert detect_ats_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://stripe.com/jobs/search?gh_jid=123",
        "https://example.com/careers",
        "https://www.linkedin.com/jobs/view/123456",
        "not a url",
        "",
        "https://boards.greenhouse.io/",
        "https://boards.greenhouse.io/embed/job_board",
        "https://jobs.lever.co/",
        "https://jobs.ashbyhq.com/",
    ],
)
def test_non_ats_urls_return_none(url: str) -> None:
    assert detect_ats_url(url) is None


def test_html_with_the_embed_script() -> None:
    html = (
        '<div id="grnhse_app"></div>'
        '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>'
    )
    assert find_ats_refs_in_html(html) == [AtsRef("greenhouse", "acme")]


def test_html_with_iframes_and_links() -> None:
    html = f"""
        <iframe src="//job-boards.greenhouse.io/embed/job_board?for=acme&amp;b=https%3A%2F%2Fx"></iframe>
        <a href="https://jobs.lever.co/beta/{LEVER_ID}">Apply</a>
        <a href='https://jobs.lever.co/beta'>All jobs</a>
        <a href="https://jobs.ashbyhq.com/gamma">Ashby</a>
    """
    assert find_ats_refs_in_html(html) == [
        AtsRef("greenhouse", "acme"),
        AtsRef("lever", "beta"),
        AtsRef("ashby", "gamma"),
    ]


def test_html_without_ats_links() -> None:
    assert find_ats_refs_in_html("<a href='https://example.com/jobs'>Jobs</a>") == []


def test_gh_jid() -> None:
    assert gh_jid("https://stripe.com/jobs/search?gh_jid=8172487") == "8172487"
    assert gh_jid("https://acme.example/careers?gh_jid=abc") is None
    assert gh_jid("https://acme.example/careers") is None
