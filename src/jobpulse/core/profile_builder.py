"""The wizard's answers turned into profile sections (M8): title, level and description rules,
locations, pay, dream companies, searches, the blocklist and the notification channel.

``build_rules`` is deterministic: the same answers always give the same sections, and search
names come from their term, location and site, so regenerating never resets a baseline.
Sections are plain JSON data keyed by their dotted path; ``config`` validates them.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jobpulse.core.countries import country_name
from jobpulse.core.places import METRO_PREFIX, place_country, place_label
from jobpulse.core.roles import (
    DEFAULT_TITLE_EXCLUDES,
    FAMILIES_BY_KEY,
    LEVELS_BY_KEY,
    ROLE_FAMILIES,
    Level,
    RoleFamily,
)
from jobpulse.core.skills import ALSO_WEIGHT, MAIN_WEIGHT, SKILLS

MAX_LINKEDIN_SEARCHES = 4
MAX_INDEED_SEARCHES = 8
MAX_WORKDAY_KEYWORDS = 5
LINKEDIN_RESULTS = 25
INDEED_RESULTS = 30
SEARCH_HOURS = 3
MAX_STACK_SATURATION = 3.0
LEVEL_BONUS = 0.2
# What LinkedIn and Indeed understand for each metro area.
METRO_SEARCH_LOCATIONS = {
    "bay-area": "San Francisco Bay Area",
    "nyc": "New York, NY",
    "seattle": "Seattle, WA",
    "dc": "Washington, DC",
    "boston": "Boston, MA",
    "la": "Los Angeles, CA",
    "dfw": "Dallas, TX",
    "austin": "Austin, TX",
    "denver": "Denver, CO",
    "triangle": "Raleigh, NC",
    "atlanta": "Atlanta, GA",
    "chicago": "Chicago, IL",
}
# Indeed's country names (JobSpy's country_indeed) where they differ from ours.
INDEED_COUNTRIES = {"US": "USA", "GB": "UK"}

# Every section ``build_rules`` writes, by dotted path in the profile.
SECTIONS: tuple[str, ...] = (
    "filters.title",
    "filters.level",
    "filters.description",
    "filters.locations",
    "filters.pay",
    "scoring.dream_companies",
    "notify.channels",
    "searches",
    "blocklist.companies",
    "hide_agencies",
)


@dataclass(frozen=True, slots=True)
class Answers:
    """What the wizard asks. ``skills`` maps a catalog skill key to "main" or "also"."""

    roles: tuple[str, ...] = ()
    level: str = "mid"
    max_years: int | None = None  # None: the level's own
    countries: tuple[str, ...] = ("US",)
    places: tuple[str, ...] = ()  # place keys: "US-TX", "metro:bay-area", "US-TX/Austin"
    remote_ok: bool = True
    min_pay: int | None = None  # thousands of US dollars a year
    skills: Mapping[str, str] = field(default_factory=dict)
    dream: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()
    hide_agencies: bool = True
    channel: str = "ntfy"  # "ntfy", "discord" or "none"


def _families(answers: Answers) -> list[RoleFamily]:
    """The chosen families, strongest first, catalog order among equals."""
    chosen = [family for family in ROLE_FAMILIES if family.key in set(answers.roles)]
    return sorted(chosen, key=lambda family: -family.weight)


def title_rules(answers: Answers) -> dict[str, Any]:
    """Every chosen family's patterns, strongest first, scaled so the strongest weighs 1."""
    rules = [rule for family in _families(answers) for rule in family.titles]
    rules.sort(key=lambda rule: -rule.weight)  # stable: catalog order among equal weights
    top = max((rule.weight for rule in rules), default=1.0)
    return {
        "include": [
            {"pattern": rule.pattern, "weight": round(rule.weight / top, 2)} for rule in rules
        ],
        "exclude": list(DEFAULT_TITLE_EXCLUDES),
    }


def _level(answers: Answers) -> Level:
    return LEVELS_BY_KEY.get(answers.level, LEVELS_BY_KEY["mid"])


def level_rules(answers: Answers) -> dict[str, Any]:
    level = _level(answers)
    return {
        "include": list(level.include),
        "exclude": list(level.exclude),
        "include_bonus": LEVEL_BONUS,
    }


def stack_keywords(answers: Answers) -> dict[str, float]:
    weights = {"main": MAIN_WEIGHT, "also": ALSO_WEIGHT}
    return {
        skill.pattern: weights[answers.skills[skill.key]]
        for skill in SKILLS
        if answers.skills.get(skill.key) in weights
    }


def description_rules(answers: Answers) -> dict[str, Any]:
    level = _level(answers)
    stack = stack_keywords(answers)
    saturation = min(MAX_STACK_SATURATION, max(1.0, sum(stack.values()) / 3))
    return {
        "max_years_experience": answers.max_years
        if answers.max_years is not None
        else level.max_years,
        "years_mode": level.years_mode,
        "level_phrases_exclude": [],
        "stack_keywords": stack,
        "stack_saturation": round(saturation, 2),
    }


def location_rules(answers: Answers) -> dict[str, Any]:
    return {
        "include": [],
        "exclude": [],
        "countries": list(answers.countries),
        "places": list(answers.places),
        "remote_ok": answers.remote_ok,
        "aliases": {},
        "unknown_location_passes": True,
    }


def _search_location(key: str) -> str:
    if key.startswith(METRO_PREFIX):
        slug = key[len(METRO_PREFIX) :]
        return METRO_SEARCH_LOCATIONS.get(slug, place_label(key))
    return place_label(key)


def search_locations(answers: Answers) -> list[tuple[str | None, str | None]]:
    """(location, country code) pairs to search: the places, then the countries (remote
    postings are listed under the country, so with places they come only when remote is
    fine)."""
    found: list[tuple[str | None, str | None]] = [
        (_search_location(key), place_country(key)) for key in answers.places
    ]
    if not answers.places or answers.remote_ok:
        found += [(country_name(code), code) for code in answers.countries]
    return found or [(None, None)]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _terms(families: Sequence[RoleFamily]) -> list[str]:
    """Each family's first term, then each one's second, and so on."""
    terms: list[str] = []
    depth = max((len(family.search_terms) for family in families), default=0)
    for index in range(depth):
        for family in families:
            if index < len(family.search_terms) and family.search_terms[index] not in terms:
                terms.append(family.search_terms[index])
    return terms


def searches(answers: Answers) -> list[dict[str, Any]]:
    """LinkedIn and Indeed searches: each term in each location, at most
    ``MAX_LINKEDIN_SEARCHES`` and ``MAX_INDEED_SEARCHES``. Terms and locations interleave
    (the strongest term in the first location, then the next term there, then the first term
    in the next location…), so a cap keeps both kinds of variety."""
    terms = _terms(_families(answers))
    places = search_locations(answers)
    ranked = sorted(
        ((t, p) for t in range(len(terms)) for p in range(len(places))),
        key=lambda pair: (pair[0] + pair[1], pair[1]),
    )
    pairs = [(terms[t], *places[p]) for t, p in ranked]
    found: list[dict[str, Any]] = []
    for site, cap, results in (
        ("linkedin", MAX_LINKEDIN_SEARCHES, LINKEDIN_RESULTS),
        ("indeed", MAX_INDEED_SEARCHES, INDEED_RESULTS),
    ):
        for term, location, country in pairs[:cap]:
            name = "-".join(part for part in (_slug(term), _slug(location or ""), site) if part)
            entry: dict[str, Any] = {
                "name": name,
                "site": site,
                "search_term": term,
                "location": location,
                "hours_old": SEARCH_HOURS,
                "results_wanted": results,
            }
            if site == "indeed" and country:
                entry["country_indeed"] = INDEED_COUNTRIES.get(country, country_name(country))
            found.append(entry)
    return found


def workday_keywords(answers: Answers) -> tuple[str, ...]:
    """Search keywords for Workday boards, strongest families first."""
    keywords: list[str] = []
    for family in _families(answers):
        for keyword in family.workday_keywords:
            if keyword not in keywords:
                keywords.append(keyword)
    return tuple(keywords[:MAX_WORKDAY_KEYWORDS])


def build_rules(answers: Answers) -> dict[str, Any]:
    """Every section in ``SECTIONS``, by dotted path."""
    return {
        "filters.title": title_rules(answers),
        "filters.level": level_rules(answers),
        "filters.description": description_rules(answers),
        "filters.locations": location_rules(answers),
        "filters.pay": {"min_yearly_usd": answers.min_pay * 1000 if answers.min_pay else None},
        "scoring.dream_companies": list(answers.dream),
        "notify.channels": [answers.channel] if answers.channel in ("ntfy", "discord") else [],
        "searches": searches(answers),
        "blocklist.companies": list(answers.avoid),
        "hide_agencies": answers.hide_agencies,
    }


def known_roles(keys: Sequence[str]) -> list[str]:
    return [key for key in keys if key in FAMILIES_BY_KEY]
