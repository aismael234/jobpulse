"""Item 11: title, level, years-of-experience and location rules behave as configured."""

import pytest

from jobpulse.core.filters import (
    FilterRules,
    LocationRules,
    check_description,
    check_locations,
    check_title,
    extract_years_required,
)
from tests.factories import compiled, weighted

RULES = FilterRules(
    title_include=weighted(
        (r"\bback[- ]?end\b", 1.0), (r"\bplatform\b", 0.8), (r"\bsoftware engineer\b", 0.6)
    ),
    title_exclude=compiled(r"\bmanager\b", r"\bintern\b"),
    level_include=compiled(r"\bsenior\b"),
    level_exclude=compiled(r"\bstaff\b", r"\bprincipal\b"),
    level_include_bonus=0.2,
)


@pytest.mark.parametrize(
    ("title", "passed", "match"),
    [
        ("Backend Engineer", True, 1.0),
        ("Back-end Engineer", True, 1.0),
        ("Senior Platform Engineer", True, 1.0),
        ("Platform Engineer", True, 0.8),
        ("Software Engineer, Payments", True, 0.6),
        ("Engineering Manager, Backend", False, 0.0),
        ("Backend Intern", False, 0.0),
        ("Staff Backend Engineer", False, 0.0),
        ("Product Designer", False, 0.0),
    ],
)
def test_title_rules(title: str, passed: bool, match: float) -> None:
    verdict = check_title(title, RULES)
    assert verdict.passed is passed
    assert verdict.match == pytest.approx(match)


def test_title_rejections_name_the_pattern() -> None:
    assert "manager" in check_title("Engineering Manager", RULES).reason
    assert "staff" in check_title("Staff Platform Engineer", RULES).reason


def test_without_include_rules_any_title_passes() -> None:
    verdict = check_title("Anything At All", FilterRules())
    assert verdict.passed
    assert verdict.match == 1.0


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("3+ years of experience building APIs", 3),
        ("You have 3-5 years of professional experience.", 3),
        ("Minimum of 3 years building distributed systems", 3),
        ("At least 2 years with Go", 2),
        ("5+ years of experience", 5),
        ("3+ years building backend services", 3),
        ("2+ years with Kubernetes and 5+ years of software engineering experience", 5),
        ("Experience: 4 years in backend development", 4),
        ("We have been in business for 10 years.", None),
        ("A 4-year degree in computer science", None),
        ("", None),
        (None, None),
    ],
)
def test_years_required(text: str | None, years: int | None) -> None:
    assert extract_years_required(text) == years


@pytest.mark.parametrize("text", ["3+ years of experience", "3-5 years of experience"])
def test_years_within_the_max_pass_without_penalty(text: str) -> None:
    verdict = check_description(text, FilterRules(max_years=4))
    assert verdict.passed
    assert verdict.level_mismatch == 0.0


def test_years_above_the_max_are_penalized_by_default() -> None:
    verdict = check_description("5+ years of experience", FilterRules(max_years=4))
    assert verdict.passed
    assert verdict.years_required == 5
    assert verdict.level_mismatch == 1.0
    assert "5" in verdict.reason


def test_years_above_the_max_are_excluded_in_exclude_mode() -> None:
    verdict = check_description(
        "5+ years of experience", FilterRules(max_years=4, years_mode="exclude")
    )
    assert not verdict.passed
    assert "5" in verdict.reason


def test_level_phrase_in_description_is_a_mismatch() -> None:
    rules = FilterRules(level_phrases_exclude=compiled(r"staff-level"))
    verdict = check_description("This is a staff-level role.", rules)
    assert verdict.passed
    assert verdict.level_mismatch == 1.0


def test_stack_keywords_are_weighted_and_saturate() -> None:
    rules = FilterRules(
        stack=weighted((r"\bpython\b", 1.0), (r"\bpostgres(ql)?\b", 0.5), (r"\brust\b", 1.0)),
        stack_saturation=2.0,
    )
    verdict = check_description("Python services on PostgreSQL", rules)
    assert verdict.stack_match == pytest.approx(0.75)
    assert set(verdict.stack_hits) == {r"\bpython\b", r"\bpostgres(ql)?\b"}
    full = check_description("Python, Rust and Postgres", rules)
    assert full.stack_match == 1.0


def test_missing_description_is_neutral() -> None:
    rules = FilterRules(max_years=1, years_mode="exclude", stack=weighted((r"\bpython\b", 1.0)))
    verdict = check_description(None, rules)
    assert verdict.passed
    assert verdict.stack_match is None
    assert verdict.level_mismatch == 0.0


def test_no_stack_rules_means_no_stack_signal() -> None:
    assert check_description("Python", FilterRules()).stack_match is None


LOCATION_RULES = LocationRules(
    include=compiled(r"^Remote \(US\)$", r"San Francisco"), exclude=compiled(r"\bIndia\b")
)


def test_location_include() -> None:
    assert not check_locations(["London, UK"], LOCATION_RULES).passed
    assert check_locations(["London, UK", "Remote (US)"], LOCATION_RULES).passed


def test_location_exclude() -> None:
    assert not check_locations(
        ["Bangalore, India"], LocationRules(exclude=compiled(r"\bIndia\b"))
    ).passed


def test_unknown_location() -> None:
    assert check_locations([], LOCATION_RULES).passed
    assert not check_locations([], LocationRules(unknown_passes=False)).passed


def test_empty_location_rules_accept_anything() -> None:
    assert check_locations(["Anywhere, Earth"], LocationRules()).passed
