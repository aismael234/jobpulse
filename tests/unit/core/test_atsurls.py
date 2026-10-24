"""Item 9 (ATS forms): every Greenhouse, Lever, Ashby and Workday URL form is recognized."""

import pytest

from jobpulse.core.atsurls import (
    AtsRef,
    WorkdayBoard,
    board_url,
    detect_ats_url,
    detect_workday_url,
    find_ats_refs_in_html,
    gh_jid,
    normalize_workday_token,
    workday_board,
)

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


@pytest.mark.parametrize(
    ("ats", "token", "region", "expected"),
    [
        ("greenhouse", "stripe", "us", "https://job-boards.greenhouse.io/stripe"),
        ("greenhouse", "proton", "eu", "https://job-boards.eu.greenhouse.io/proton"),
        ("lever", "spotify", "us", "https://jobs.lever.co/spotify"),
        ("lever", "olx", "eu", "https://jobs.eu.lever.co/olx"),
        ("ashby", "Linear", "us", "https://jobs.ashbyhq.com/Linear"),
        ("workday", "acme", "us", None),
    ],
)
def test_board_url_points_at_the_public_board(
    ats: str, token: str, region: str, expected: str | None
) -> None:
    assert board_url(ats, token, region) == expected
    if expected is not None:  # and the link reads back as the same board
        ref = detect_ats_url(expected)
        assert ref is not None
        assert (ref.ats, ref.token, ref.region) == (
            ats,
            token.lower() if ats != "ashby" else token,
            region,
        )


# --- Workday (M4) ----------------------------------------------------------------------

NVIDIA = "nvidia.wd5/NVIDIAExternalCareerSite"
NVIDIA_JOB = "RTL-Design-Engineer--DFT_JR2028303"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
            AtsRef("workday", NVIDIA),
        ),
        (
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/",
            AtsRef("workday", NVIDIA),
        ),
        (
            "https://NVIDIA.WD5.myworkdayjobs.com/NVIDIAExternalCareerSite?q=backend",
            AtsRef("workday", NVIDIA),
        ),
        (
            f"https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/Israel-Yokneam/{NVIDIA_JOB}",
            AtsRef("workday", NVIDIA, NVIDIA_JOB),
        ),
        (
            f"https://nvidia.wd5.myworkdayjobs.com/en-us/NVIDIAExternalCareerSite/job/Israel-Yokneam/{NVIDIA_JOB}/apply",
            AtsRef("workday", NVIDIA, NVIDIA_JOB),
        ),
        (
            f"https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/details/{NVIDIA_JOB}",
            AtsRef("workday", NVIDIA, NVIDIA_JOB),
        ),
        (
            "https://wd5.myworkdaysite.com/recruiting/microchiphr/External",
            AtsRef("workday", "microchiphr.wd5/External"),
        ),
        (
            "https://wd5.myworkdaysite.com/en-US/recruiting/microchiphr/External/job/"
            "United-Kingdom---Whiteley/Graduate-R-D-Engineer_R2515-26",
            AtsRef("workday", "microchiphr.wd5/External", "Graduate-R-D-Engineer_R2515-26"),
        ),
        (
            "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs",
            AtsRef("workday", NVIDIA),
        ),
        (
            f"https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/job/x/{NVIDIA_JOB}",
            AtsRef("workday", NVIDIA, NVIDIA_JOB),
        ),
    ],
)
def test_detects_every_workday_form(url: str, expected: AtsRef) -> None:
    assert detect_workday_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://nvidia.wd5.myworkdayjobs.com/",
        "https://nvidia.wd5.myworkdayjobs.com/en-US/",
        "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia",
        "https://wd5.myworkdaysite.com/recruiting/microchiphr",
        "https://www.myworkdayjobs.com/External",
        "https://nvidia.myworkdayjobs.com/External",
        "https://job-boards.greenhouse.io/acme",
        "https://example.com/External/job/x/y_R1",
        "",
    ],
)
def test_non_workday_urls_return_none(url: str) -> None:
    assert detect_workday_url(url) is None


def test_discovery_detection_leaves_workday_alone() -> None:
    """Discovery uses ``detect_ats_url``, and only you add Workday boards."""
    assert detect_ats_url("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (NVIDIA, NVIDIA),
        ("NVIDIA.WD5/NVIDIAExternalCareerSite", NVIDIA),
        ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite", NVIDIA),
        (
            f"https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/x/{NVIDIA_JOB}",
            NVIDIA,
        ),
        ("  workday.wd5/Workday  ", "workday.wd5/Workday"),
        ("nvidia", None),
        ("nvidia/External", None),
        ("nvidia.wd5", None),
        ("nvidia.wd5/", None),
        ("nvidia.wdx/External", None),
        ("https://example.com/External", None),
        ("", None),
    ],
)
def test_normalize_workday_token(value: str, expected: str | None) -> None:
    assert normalize_workday_token(value) == expected


def test_workday_board_urls() -> None:
    board = workday_board("omega.wd5/External")
    assert board == WorkdayBoard("omega", "wd5", "External")
    assert board.token == "omega.wd5/External"
    assert board.board_url == "https://omega.wd5.myworkdayjobs.com/External"
    assert board.api_url == "https://omega.wd5.myworkdayjobs.com/wday/cxs/omega/External"
    assert workday_board("not a token") is None


def test_board_url_for_a_workday_token() -> None:
    assert board_url("workday", "omega.wd5/External") == (
        "https://omega.wd5.myworkdayjobs.com/External"
    )
    ref = detect_workday_url("https://omega.wd5.myworkdayjobs.com/External")
    assert ref == AtsRef("workday", "omega.wd5/External")


def test_html_with_a_workday_board_link() -> None:
    html = """
        <a href="https://omega.wd5.myworkdayjobs.com/en-US/External">Open roles</a>
        <a href="https://omega.wd5.myworkdayjobs.com/External/job/Seattle-WA/SWE_R1">SWE</a>
        <a href="https://jobs.lever.co/beta">Lever</a>
    """
    assert find_ats_refs_in_html(html) == [
        AtsRef("workday", "omega.wd5/External"),
        AtsRef("lever", "beta"),
    ]
