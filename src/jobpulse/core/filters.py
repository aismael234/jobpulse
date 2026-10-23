"""Title, description and location rules. Patterns arrive compiled (case-insensitive)."""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class WeightedRegex:
    pattern: re.Pattern[str]
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class LocationRules:
    include: tuple[re.Pattern[str], ...] = ()
    exclude: tuple[re.Pattern[str], ...] = ()
    unknown_passes: bool = True


@dataclass(frozen=True, slots=True)
class FilterRules:
    title_include: tuple[WeightedRegex, ...] = ()
    title_exclude: tuple[re.Pattern[str], ...] = ()
    level_include: tuple[re.Pattern[str], ...] = ()
    level_exclude: tuple[re.Pattern[str], ...] = ()
    level_include_bonus: float = 0.2
    max_years: int | None = None
    years_mode: Literal["penalize", "exclude"] = "penalize"
    level_phrases_exclude: tuple[re.Pattern[str], ...] = ()
    stack: tuple[WeightedRegex, ...] = ()
    stack_saturation: float = 2.0
    locations: LocationRules = field(default_factory=LocationRules)


@dataclass(frozen=True, slots=True)
class TitleVerdict:
    passed: bool
    reason: str
    match: float = 0.0


@dataclass(frozen=True, slots=True)
class DescriptionVerdict:
    passed: bool
    reason: str
    years_required: int | None = None
    level_mismatch: float = 0.0
    stack_match: float | None = None
    stack_hits: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LocationVerdict:
    passed: bool
    reason: str


# "3+ years", "3-5 years", "minimum of 3 years", "at least 2 yrs", plain "4 years".
_YEARS = re.compile(
    r"(?P<min_phrase>\b(?:minimum(?:\s+of)?|at\s+least|min\.?)\s+)?"
    r"(?P<low>\d{1,2})\s*(?P<plus>\+)?\s*"
    r"(?:(?:-|–|—|to)\s*(?P<high>\d{1,2})\s*\+?\s*)?"
    r"(?:years?|yrs?)\b",
    re.IGNORECASE,
)
_EXPERIENCE = re.compile(r"\bexperience\b", re.IGNORECASE)
_MAX_PLAUSIBLE_YEARS = 30
_CONTEXT_BEFORE = 60
_CONTEXT_AFTER = 80


def check_title(title: str, rules: FilterRules) -> TitleVerdict:
    """Excludes win; then at least one include must match (when there are any).

    The best include weight is the title score; a level-include match adds a bonus.
    """
    for pattern in rules.title_exclude:
        if pattern.search(title):
            return TitleVerdict(False, f"title matches exclude rule {pattern.pattern!r}")
    for pattern in rules.level_exclude:
        if pattern.search(title):
            return TitleVerdict(False, f"title matches level exclude rule {pattern.pattern!r}")
    if rules.title_include:
        weights = [rule.weight for rule in rules.title_include if rule.pattern.search(title)]
        if not weights:
            return TitleVerdict(False, "title matches no include rule")
        match = max(weights)
    else:
        match = 1.0
    if any(pattern.search(title) for pattern in rules.level_include):
        match = min(1.0, match + rules.level_include_bonus)
    return TitleVerdict(True, "title matches", match)


def extract_years_required(text: str | None) -> int | None:
    """The largest minimum years-of-experience requirement stated in ``text``.

    "N+", ranges and "minimum of"/"at least" count on their own. A plain "N years" counts
    only near the word "experience", so "in business for 10 years" is ignored.
    """
    if not text:
        return None
    found: list[int] = []
    for match in _YEARS.finditer(text):
        low = int(match["low"])
        if low > _MAX_PLAUSIBLE_YEARS:
            continue
        explicit = bool(match["plus"] or match["high"] or match["min_phrase"])
        if not explicit:
            window = text[max(0, match.start() - _CONTEXT_BEFORE) : match.end() + _CONTEXT_AFTER]
            if not _EXPERIENCE.search(window):
                continue
        found.append(low)
    return max(found) if found else None


def check_description(text: str | None, rules: FilterRules) -> DescriptionVerdict:
    """Years of experience, level phrases and stack keywords. No description is neutral."""
    if text is None or not text.strip():
        return DescriptionVerdict(True, "no description")
    notes: list[str] = []
    mismatch = 0.0
    years = extract_years_required(text)
    if rules.max_years is not None and years is not None and years > rules.max_years:
        note = f"asks for {years}+ years (max {rules.max_years})"
        if rules.years_mode == "exclude":
            return DescriptionVerdict(False, note, years, 1.0)
        notes.append(note)
        mismatch = 1.0
    for pattern in rules.level_phrases_exclude:
        if pattern.search(text):
            notes.append(f"description matches level phrase {pattern.pattern!r}")
            mismatch = 1.0
            break
    stack_match: float | None = None
    hits: tuple[str, ...] = ()
    if rules.stack:
        matched = [rule for rule in rules.stack if rule.pattern.search(text)]
        hits = tuple(rule.pattern.pattern for rule in matched)
        stack_match = min(1.0, sum(rule.weight for rule in matched) / rules.stack_saturation)
    return DescriptionVerdict(
        True, "; ".join(notes) or "description ok", years, mismatch, stack_match, hits
    )


def check_locations(labels: Sequence[str], rules: LocationRules) -> LocationVerdict:
    """Passes when at least one location is included (or no includes) and not excluded."""
    if not labels:
        if rules.unknown_passes:
            return LocationVerdict(True, "no location given")
        return LocationVerdict(False, "no location given (unknown_location_passes is off)")
    for label in labels:
        included = not rules.include or any(p.search(label) for p in rules.include)
        if included and not any(p.search(label) for p in rules.exclude):
            return LocationVerdict(True, f"location ok ({label})")
    return LocationVerdict(False, f"no acceptable location in {', '.join(labels)}")
