"""Location normalization: split multi-city strings, recognize remote forms, apply aliases.

Labels are plain strings such as "San Francisco, CA", "Remote (US)" or "Remote". The alias
table is deliberately small; ``filters.locations.aliases`` in the profile extends it.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from jobpulse.core.text import collapse_whitespace


@dataclass(frozen=True, slots=True)
class NormalizedLocation:
    label: str
    is_remote: bool
    region: str | None = None


# Keys are casefolded.
DEFAULT_ALIASES: dict[str, str] = {
    "sf": "San Francisco, CA",
    "san francisco": "San Francisco, CA",
    "san francisco, ca": "San Francisco, CA",
    "san francisco, california": "San Francisco, CA",
    "bay area": "San Francisco Bay Area",
    "sf bay area": "San Francisco Bay Area",
    "nyc": "New York, NY",
    "new york": "New York, NY",
    "new york city": "New York, NY",
    "new york, ny": "New York, NY",
    "new york, new york": "New York, NY",
    "la": "Los Angeles, CA",
    "los angeles": "Los Angeles, CA",
    "seattle": "Seattle, WA",
    "austin": "Austin, TX",
    "boston": "Boston, MA",
    "chicago": "Chicago, IL",
    "denver": "Denver, CO",
    "dc": "Washington, DC",
    "washington dc": "Washington, DC",
    "washington, d.c.": "Washington, DC",
}
DEFAULT_SHORT_LABELS: dict[str, str] = {
    "San Francisco, CA": "SF",
    "New York, NY": "NYC",
    "Los Angeles, CA": "LA",
    "Washington, DC": "DC",
    "San Francisco Bay Area": "Bay Area",
}
_REMOTE_REGIONS: dict[str, str] = {
    "us": "US",
    "usa": "US",
    "u.s.": "US",
    "u.s": "US",
    "u.s.a.": "US",
    "united states": "US",
    "united states of america": "US",
    "canada": "Canada",
    "uk": "UK",
    "united kingdom": "UK",
    "eu": "EU",
    "europe": "Europe",
    "emea": "EMEA",
    "apac": "APAC",
    "latam": "LATAM",
    "americas": "Americas",
    "north america": "North America",
    "global": "Global",
    "worldwide": "Worldwide",
    "anywhere": "Anywhere",
}

_REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)
_HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)
_FILLER_WORDS = re.compile(
    r"\b(?:in|within|the|only|based|first|friendly|ok|okay|eligible|from)\b", re.IGNORECASE
)
_EDGE_CHARS = " -–—,:/|()[]"
_REGION_SPLIT = re.compile(r"\s*(?:/|,|&|\bor\b)\s*", re.IGNORECASE)
_WORD_SEPARATOR = re.compile(r"\s+(?:or|&)\s+", re.IGNORECASE)
_CHAR_SEPARATORS = (";", "|", "•", "\n", "/")
_CITY_STATE_PAIRS = re.compile(r"(?:[^,]+,\s*[A-Z]{2}\s*,\s*)+[^,]+,\s*[A-Z]{2}")
_CITY_STATE_PAIR = re.compile(r"([^,]+,\s*[A-Z]{2})\s*(?:,|$)")
# "San Francisco, CA, USA" (JobSpy) or "Austin, Texas, United States": the city and state
# are enough, and match how the ATS boards write US locations.
_US_COUNTRY_SUFFIX = re.compile(
    r"(?P<place>[^,]+,[^,]+?)\s*,\s*(?:USA|US|U\.S\.A?\.?|United States(?: of America)?)",
    re.IGNORECASE,
)
_PUNCTUATION = re.compile(r"[^\w\s]")


def merge_aliases(user_aliases: Mapping[str, str]) -> dict[str, str]:
    """The built-in table plus the user's entries (keys compared case-insensitively)."""
    return {
        **DEFAULT_ALIASES,
        **{key.strip().casefold(): value for key, value in user_aliases.items()},
    }


def _separator_length(text: str, index: int) -> int:
    for separator in _CHAR_SEPARATORS:
        if text.startswith(separator, index):
            return len(separator)
    match = _WORD_SEPARATOR.match(text, index)
    return match.end() - index if match else 0


def split_locations(raw: str) -> list[str]:
    """Split "SF / NYC", "A; B", "A | B", "A or B" and "City, ST, City, ST" into parts.

    Separators inside parentheses don't split, so "Remote (US/Canada)" stays whole.
    """
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    index = 0
    while index < len(raw):
        char = raw[index]
        if char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        if depth == 0 and (length := _separator_length(raw, index)):
            parts.append("".join(current))
            current = []
            index += length
            continue
        current.append(char)
        index += 1
    parts.append("".join(current))

    pieces: list[str] = []
    for part in (p.strip() for p in parts):
        if _CITY_STATE_PAIRS.fullmatch(part):
            pieces.extend(pair.strip() for pair in _CITY_STATE_PAIR.findall(part))
        elif part:
            pieces.append(part)
    return pieces


def _region_name(part: str) -> str:
    known = _REMOTE_REGIONS.get(part.casefold())
    if known:
        return known
    return part if any(char.isupper() for char in part) else part.title()


def _remote_region(text: str) -> str | None:
    remainder = _FILLER_WORDS.sub(" ", _REMOTE.sub(" ", text))
    remainder = remainder.split(":")[0].split(";")[0]
    remainder = collapse_whitespace(remainder).strip(_EDGE_CHARS).strip()
    if not remainder:
        return None
    parts = [p.strip(_EDGE_CHARS) for p in _REGION_SPLIT.split(remainder)]
    regions = [_region_name(p) for p in parts if p]
    return "/".join(dict.fromkeys(regions)) or None


def normalize_location(
    raw: str, aliases: Mapping[str, str] = DEFAULT_ALIASES
) -> NormalizedLocation | None:
    text = collapse_whitespace(raw)
    if not text:
        return None
    if _REMOTE.search(text):
        region = _remote_region(text)
        return NormalizedLocation(f"Remote ({region})" if region else "Remote", True, region)
    if _HYBRID.search(text):
        text = collapse_whitespace(_HYBRID.sub(" ", text)).strip(_EDGE_CHARS).strip()
        if not text:
            return None
    if match := _US_COUNTRY_SUFFIX.fullmatch(text):
        text = match["place"].strip()
    alias = aliases.get(text.casefold())
    return NormalizedLocation(alias or text, False)


def normalize_locations(
    raws: Iterable[str | None], aliases: Mapping[str, str] = DEFAULT_ALIASES
) -> tuple[tuple[str, ...], bool | None]:
    """Unique labels in order, and whether any is remote (``None`` with no locations)."""
    labels: list[str] = []
    remote = False
    for raw in raws:
        if not raw or not raw.strip():
            continue
        for piece in split_locations(raw):
            location = normalize_location(piece, aliases)
            if location is None:
                continue
            remote = remote or location.is_remote
            if location.label not in labels:
                labels.append(location.label)
    if not labels:
        return (), None
    return tuple(labels), remote


def short_label(label: str, short_labels: Mapping[str, str] = DEFAULT_SHORT_LABELS) -> str:
    """A compact form for push titles: "SF", "Remote", "Remote Europe", "London"."""
    if label in short_labels:
        return short_labels[label]
    if label in ("Remote", "Remote (US)"):
        return "Remote"
    if label.startswith("Remote (") and label.endswith(")"):
        return f"Remote {label[len('Remote (') : -1]}"
    return label.split(",")[0].strip()


def location_summary(
    labels: Sequence[str],
    *,
    max_items: int = 2,
    short_labels: Mapping[str, str] = DEFAULT_SHORT_LABELS,
) -> str:
    """ "SF / Remote", or "SF / NYC +2" when there are more."""
    shorts = list(dict.fromkeys(short_label(label, short_labels) for label in labels))
    head = " / ".join(shorts[:max_items])
    extra = len(shorts) - max_items
    return f"{head} +{extra}" if extra > 0 else head


def location_key(label: str | None) -> str:
    """City-level key for fingerprints, so "San Francisco, CA" and
    "San Francisco, California, United States" agree. Any remote label is "remote"."""
    if not label:
        return ""
    if _REMOTE.search(label):
        return "remote"
    city = label.split(",")[0].casefold()
    return collapse_whitespace(_PUNCTUATION.sub(" ", city))
