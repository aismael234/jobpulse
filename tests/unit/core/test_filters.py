"""Item 11: title, level, years-of-experience and location rules behave as configured."""

import pytest

from jobpulse.core.filters import (
    Blocklist,
    FilterRules,
    LocationRules,
    check_description,
    check_locations,
    check_pay,
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


# --- countries ------------------------------------------------------------------------

US_ONLY = LocationRules(countries=frozenset({"US"}))


def test_countries_keep_postings_in_them() -> None:
    assert check_locations(["San Francisco, CA"], US_ONLY).passed
    assert check_locations(["London", "Remote (US)"], US_ONLY).passed
    verdict = check_locations(["Bangalore, Karnataka, India"], US_ONLY)
    assert not verdict.passed
    assert verdict.reason == "no location in US (Bangalore, Karnataka, India)"


def test_countries_and_remote_regions() -> None:
    assert check_locations(["Remote (Global)"], US_ONLY).passed
    assert check_locations(["Remote (North America)"], US_ONLY).passed
    assert not check_locations(["Remote (EMEA)"], US_ONLY).passed
    assert not check_locations(["Remote (Canada)"], US_ONLY).passed


def test_unknown_countries_follow_unknown_location_passes() -> None:
    assert check_locations(["Remote"], US_ONLY).passed
    strict = LocationRules(countries=frozenset({"US"}), unknown_passes=False)
    verdict = check_locations(["Remote", "Main Campus"], strict)
    assert not verdict.passed
    assert "unknown_location_passes" in verdict.reason


def test_a_label_without_a_country_sides_with_the_others() -> None:
    """Next to a foreign city, an unknown label is taken to be abroad too; next to a US
    city, it counts as one of the posting's US locations."""
    assert not check_locations(["Pune", "Pimpri-Chinchwad Area"], US_ONLY).passed
    assert not check_locations(["London", "Remote"], US_ONLY).passed
    remote_us = LocationRules(include=compiled(r"^Remote\b"), countries=frozenset({"US"}))
    assert check_locations(["San Francisco, CA", "Remote"], remote_us).passed
    assert not check_locations(["London", "Remote"], remote_us).passed
    assert check_locations(["Singapore", "US > Arizona > Phoenix"], US_ONLY).passed


def test_excludes_still_apply_inside_the_countries() -> None:
    rules = LocationRules(exclude=compiled(r"San Francisco"), countries=frozenset({"US"}))
    assert not check_locations(["San Francisco, CA"], rules).passed
    assert check_locations(["San Francisco, CA", "Austin, TX"], rules).passed


def test_several_countries() -> None:
    rules = LocationRules(countries=frozenset({"US", "CA"}))
    assert check_locations(["Toronto, Ontario, Canada"], rules).passed
    verdict = check_locations(["London, UK"], rules)
    assert verdict.reason == "no location in CA, US (London, UK)"


# The places of the postings you dismissed on 2026-10-24 and 25 for being outside the US,
# and of every posting you applied to.
DISMISSED_ABROAD = [
    ("Nigeria",),
    ("Lousado, Braga, Portugal",),
    ("Shanghai, Shanghai, China",),
    ("London, England, UK",),
    ("India",),
    ("Seoul, South Korea",),
    ("Ontario, Canada",),
    ("Toronto, Ontario, Canada",),
    ("Montreal, Quebec, Canada",),
    ("Ho Chi Minh City, Vietnam",),
    ("Pune", "Pimpri-Chinchwad Area"),
    ("Beer Yaakov, Center District, Israel",),
    ("Singapore, Singapore",),
    ("Jakarta, Indonesia",),
    ("Tokyo, Japan",),
    ("Japan",),
    ("Barcelona, Catalonia, Spain",),
]
APPLIED = [
    ("Remote (Global)",),
    ("San Jose, CA",),
    ("San Leandro, CA",),
    ("San Francisco Bay Area",),
    ("San Francisco, CA",),
    ("Cambridge, MA",),
    ("USA",),
    ("Huntsville, AL",),
    ("Dallas, TX",),
    ("Atlanta, GA",),
    ("Research Triangle Park, NC",),
]


@pytest.mark.parametrize("labels", DISMISSED_ABROAD)
def test_us_only_drops_the_dismissed_postings_abroad(labels: tuple[str, ...]) -> None:
    assert not check_locations(labels, US_ONLY).passed


@pytest.mark.parametrize("labels", APPLIED)
def test_us_only_keeps_every_applied_posting(labels: tuple[str, ...]) -> None:
    assert check_locations(labels, US_ONLY).passed


# --- the company blocklist ---------------------------------------------------------------


def test_blocklist_matches_names_without_case_punctuation_or_legal_suffixes() -> None:
    blocklist = Blocklist.build(companies=["Acme, Inc.", "Jobot", "Meta"])
    assert blocklist.blocks_company("ACME")
    assert blocklist.blocks_company("Acme Inc")
    assert blocklist.blocks_company("jobot")
    assert not blocklist.blocks_company("Acme Widgets")
    assert not blocklist.blocks_company("Metabase")  # whole names only, never substrings
    assert not blocklist.blocks_company("")


def test_blocklist_boards_are_ats_and_token() -> None:
    blocklist = Blocklist.build(boards=["greenhouse/SpaceX", "lever/veeva"])
    assert blocklist.blocks_board("greenhouse", "spacex")
    assert blocklist.blocks_board("lever", "Veeva")
    assert not blocklist.blocks_board("lever", "spacex")
    assert blocklist.boards == frozenset({("greenhouse", "spacex"), ("lever", "veeva")})


def test_an_empty_blocklist_blocks_nothing() -> None:
    assert not Blocklist().blocks_company("Acme")
    assert not Blocklist().blocks_board("greenhouse", "acme")


# --- M8: places, remote and the pay floor -------------------------------------------------------

PLACES = LocationRules(countries=frozenset({"US"}), places=("metro:bay-area", "US-TX/Austin"))


@pytest.mark.parametrize(
    ("labels", "passed"),
    [
        (["San Francisco, CA"], True),
        (["Austin, TX"], True),
        (["Dallas, TX"], False),
        (["Remote (US)"], True),
        (["Remote"], True),
        (["London, UK"], False),
        (["Dallas, TX", "Sunnyvale, CA"], True),
        ([], True),  # unknown location passes as before
    ],
)
def test_places_keep_postings_in_them_and_remote(labels: list[str], passed: bool) -> None:
    assert check_locations(labels, PLACES).passed is passed


def test_without_remote_ok_remote_postings_fail_places() -> None:
    rules = LocationRules(countries=frozenset({"US"}), places=("US-TX",), remote_ok=False)
    assert not check_locations(["Remote (US)"], rules).passed
    assert check_locations(["Houston, TX"], rules).passed


def test_no_places_means_anywhere_in_the_countries() -> None:
    rules = LocationRules(countries=frozenset({"US"}))
    assert check_locations(["Dallas, TX"], rules).passed


@pytest.mark.parametrize(
    ("compensation", "floor", "filtered"),
    [
        (
            {"min": 120000, "max": 140000, "currency": "USD", "interval": "per-year-salary"},
            150000,
            True,
        ),
        (
            {"min": 140000, "max": 160000, "currency": "USD", "interval": "per-year-salary"},
            150000,
            False,
        ),
        ({"min": 50, "max": 60, "currency": "USD", "interval": "hourly"}, 150000, True),
        ({"min": 100000, "max": 120000, "currency": "EUR"}, 150000, False),  # not USD: passes
        (None, 150000, False),  # unlisted: passes
        ({"min": 1, "max": 2, "currency": "USD"}, None, False),  # no floor
    ],
)
def test_pay_floor(
    compensation: dict[str, object] | None, floor: int | None, filtered: bool
) -> None:
    assert (check_pay(compensation, floor) is not None) is filtered


def test_dream_companies_compare_like_the_blocklist() -> None:
    rules = FilterRules(dream_companies=frozenset({"stripe"}))
    assert rules.is_dream("Stripe, Inc.")
    assert not rules.is_dream("Stripes Co")
    assert not FilterRules().is_dream("Stripe")
