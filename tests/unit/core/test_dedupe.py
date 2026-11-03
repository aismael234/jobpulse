"""Item 6 (pure part): URL canonicalization, ATS keys and fingerprints."""

import pytest

from jobpulse.core.dedupe import (
    ats_key,
    canonicalize_url,
    fingerprint,
    merge_also_seen,
    normalize_company,
    normalize_title,
    posting_fingerprint,
)
from tests.factories import make_posting

LEVER_ID = "5c6e2a0e-1f2b-4c3d-9e8f-0a1b2c3d4e5f"
ASHBY_ID = "d3bc1ced-3ce4-4086-a050-555055dbb1ff"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://boards.greenhouse.io/acme/jobs/4012345007?gh_src=abc123&utm_source=LinkedIn",
            "greenhouse:acme:4012345007",
        ),
        ("https://job-boards.greenhouse.io/acme/jobs/4012345007/", "greenhouse:acme:4012345007"),
        (
            f"https://jobs.lever.co/beta/{LEVER_ID}/apply?lever-source=LinkedIn&lever-origin=applied",
            f"lever:beta:{LEVER_ID}",
        ),
        (f"https://jobs.eu.lever.co/olx/{LEVER_ID}/apply", f"lever:olx:{LEVER_ID}"),
        (
            f"https://jobs.ashbyhq.com/Gamma/{ASHBY_ID}/application?utm_source=x",
            f"ashby:gamma:{ASHBY_ID}",
        ),
        (f"https://jobs.ashbyhq.com/gamma/{ASHBY_ID}?embed=true", f"ashby:gamma:{ASHBY_ID}"),
        (
            "HTTPS://Example.COM/Careers/Backend-Engineer/apply/?utm_source=x&ref=y&src=z"
            "&source=q&trk=1&refId=2&trackingId=3&id=7#section",
            "https://example.com/Careers/Backend-Engineer?id=7",
        ),
        ("https://example.com/jobs/1/application", "https://example.com/jobs/1"),
        ("https://example.com/jobs?b=2&a=1", "https://example.com/jobs?a=1&b=2"),
        ("https://example.com:443/jobs/1", "https://example.com/jobs/1"),
    ],
)
def test_canonicalize_url(url: str, expected: str) -> None:
    assert canonicalize_url(url) == expected


def test_gh_jid_on_a_known_custom_host_maps_to_the_greenhouse_key() -> None:
    hosts = {"stripe.com": "stripe"}
    assert (
        canonicalize_url(
            "https://stripe.com/jobs/search?gh_jid=8172487&gh_src=abc", greenhouse_hosts=hosts
        )
        == "greenhouse:stripe:8172487"
    )
    assert (
        canonicalize_url(
            "https://www.stripe.com/jobs/search?gh_jid=8172487#/8172487", greenhouse_hosts=hosts
        )
        == "greenhouse:stripe:8172487"
    )


def test_gh_jid_on_an_unknown_host_stays_a_url() -> None:
    assert (
        canonicalize_url("https://stripe.com/jobs/search?gh_jid=8172487&gh_src=abc")
        == "https://stripe.com/jobs/search?gh_jid=8172487"
    )


def test_ats_key_is_lowercase() -> None:
    assert ats_key("lever", "Beta", "5C6E") == "lever:beta:5c6e"


def test_empty_url() -> None:
    assert canonicalize_url("") == ""


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("Acme, Inc.", "acme"),
        ("ACME Corp", "acme"),
        ("Foo LLC", "foo"),
        ("Bar Ltd.", "bar"),
        ("Initech Corporation", "initech"),
        ("The  Widget   Company", "the widget company"),
    ],
)
def test_normalize_company(raw: str, normalized: str) -> None:
    assert normalize_company(raw) == normalized


def test_normalize_title_keeps_seniority() -> None:
    assert (
        normalize_title("Senior Software Engineer, Backend (Payments)")
        == "senior software engineer backend payments"
    )


def test_fingerprint_survives_formatting_differences() -> None:
    a = fingerprint("Acme, Inc.", "Backend Engineer – Payments", "San Francisco, CA")
    b = fingerprint("ACME", "backend engineer payments", "San Francisco, California, United States")
    assert a == b
    assert len(a) == 64


def test_fingerprint_keeps_seniority_and_location_apart() -> None:
    base = fingerprint("Acme", "Backend Engineer", "San Francisco, CA")
    assert fingerprint("Acme", "Senior Backend Engineer", "San Francisco, CA") != base
    assert fingerprint("Acme", "Backend Engineer", "New York, NY") != base


def test_posting_fingerprint_uses_the_primary_location() -> None:
    posting = make_posting(locations=("San Francisco, CA", "New York, NY"))
    assert posting_fingerprint(posting) == fingerprint(
        "Acme", "Backend Engineer", "San Francisco, CA"
    )


def test_a_posting_without_a_company_only_matches_itself() -> None:
    first = make_posting(source="indeed", external_id="a1", company_name="")
    second = make_posting(source="indeed", external_id="b2", company_name="  ")
    assert posting_fingerprint(first) != posting_fingerprint(second)
    assert posting_fingerprint(first) == posting_fingerprint(
        make_posting(source="indeed", external_id="a1", company_name="")
    )
    assert posting_fingerprint(first) != fingerprint("", "Backend Engineer", "San Francisco, CA")


def test_merge_also_seen_is_idempotent_and_pure() -> None:
    entry = {
        "source": "linkedin",
        "external_id": "4012",
        "url": "https://www.linkedin.com/jobs/view/4012",
    }
    original: list[dict[str, str]] = []
    once = merge_also_seen(original, entry)
    twice = merge_also_seen(once, entry)
    assert once == [entry]
    assert twice == [entry]
    assert original == []


# --- Workday (M4): every form of a job URL gives the same key --------------------------

OMEGA_KEY = "workday:omega.wd5/external:software-engineer---new-grad_r0012345"


@pytest.mark.parametrize(
    "url",
    [
        "https://omega.wd5.myworkdayjobs.com/External/job/Seattle-WA/Software-Engineer---New-Grad_R0012345",
        "https://omega.wd5.myworkdayjobs.com/en-US/External/job/Seattle-WA/Software-Engineer---New-Grad_R0012345?source=LinkedIn&utm_medium=x",
        "https://omega.wd5.myworkdayjobs.com/External/job/Remote-USA/Software-Engineer---New-Grad_R0012345/apply",
        "https://omega.wd5.myworkdayjobs.com/External/details/Software-Engineer---New-Grad_R0012345",
        "https://wd5.myworkdaysite.com/recruiting/omega/External/job/Seattle-WA/Software-Engineer---New-Grad_R0012345",
    ],
)
def test_workday_job_urls_canonicalize_to_one_key(url: str) -> None:
    assert canonicalize_url(url) == OMEGA_KEY


def test_a_workday_board_url_is_not_a_job_key() -> None:
    assert canonicalize_url("https://omega.wd5.myworkdayjobs.com/en-US/External/") == (
        "https://omega.wd5.myworkdayjobs.com/en-US/External"
    )


def test_the_workday_key_matches_the_adapters() -> None:
    assert ats_key("workday", "omega.wd5/External", "Software-Engineer---New-Grad_R0012345") == (
        OMEGA_KEY
    )
