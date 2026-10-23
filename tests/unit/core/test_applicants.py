"""Item 10: the applicant line in every phrasing LinkedIn uses."""

import pytest

from jobpulse.core.applicants import ApplicantCount, parse_applicants


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Be among the first 25 applicants",
            ApplicantCount("Be among the first 25 applicants", None, 25),
        ),
        ("87 applicants", ApplicantCount("87 applicants", 87, 87)),
        ("1 applicant", ApplicantCount("1 applicant", 1, 1)),
        ("Over 200 applicants", ApplicantCount("Over 200 applicants", 200, None)),
        ("Over 100 applicants", ApplicantCount("Over 100 applicants", 100, None)),
        ("200+ applicants", ApplicantCount("200+ applicants", 200, None)),
        ("1,204 applicants", ApplicantCount("1,204 applicants", 1204, 1204)),
    ],
)
def test_every_phrasing(text: str, expected: ApplicantCount) -> None:
    assert parse_applicants(text) == expected


@pytest.mark.parametrize(
    "text", [None, "", "Apply on the company website", "Posted 2 days ago", "25 applications"]
)
def test_a_missing_line_is_none(text: str | None) -> None:
    assert parse_applicants(text) is None


def test_the_line_is_found_in_surrounding_page_text() -> None:
    page = "Acme · San Francisco, CA\n 2 days ago\n  Over 200 applicants \nSee who Acme has hired"
    assert parse_applicants(page) == ApplicantCount("Over 200 applicants", 200, None)


def test_case_and_whitespace_do_not_matter() -> None:
    parsed = parse_applicants("  be among the FIRST 25\n   applicants ")
    assert parsed == ApplicantCount("be among the FIRST 25 applicants", None, 25)
