"""Employment type: full time, contract, part time or internship.

Structured fields come first, per source: Ashby ``employmentType``, Lever
``categories.commitment``, Indeed ``job_type``, Greenhouse metadata fields about employment,
Workday ``timeType``, and LinkedIn's criteria line ("Employment type: Contract") at the end of
the description its job page gave. Without one, phrases in the title and description decide,
kept tight enough to skip "smart contracts" and "government contracts". Contract-to-hire and
temporary work count as contract. No signal at all means full time.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from jobpulse.core.models import Posting

EmploymentType = Literal["full_time", "contract", "part_time", "internship"]

# One label is read in this order, so "Part-time Employee" is part time.
_LABELS: tuple[tuple[EmploymentType, re.Pattern[str]], ...] = (
    ("contract", re.compile(r"contract|temporar|\btemp\b|fixed[\s-]*term|contingent", re.I)),
    ("internship", re.compile(r"\bintern(?:ship|s)?\b|\bintr\b|working student", re.I)),
    ("part_time", re.compile(r"part[\s_-]*time", re.I)),
    ("full_time", re.compile(r"full[\s_-]*time|\bregular\b|permanent|\bemployee\b", re.I)),
)
# When a posting carries several labels ("fulltime, contract"), the first kind here wins: a
# contract-to-hire job is listed as both.
_PRECEDENCE: tuple[EmploymentType, ...] = ("contract", "internship", "full_time", "part_time")
# Greenhouse metadata is free-form per company; only fields about employment are read.
_GREENHOUSE_FIELD = re.compile(
    r"employ|time\s*type|full[\s-]*time|part[\s-]*time|regular\s+or\s+temporary|worker", re.I
)
_LINKEDIN_CRITERIA = re.compile(r"^Employment type:\s*(.+)$", re.I | re.M)

_CONTRACT_TITLE = re.compile(
    r"\bcontractor\b|\bcontract[\s-]+to[\s-]+hire\b|\btemporary\b|\btemp\b"
    r"|[(\[]\s*contract\b|\bcontract\s*[)\]]|[-–—|,:]\s*contract\s*$"
    # a leading "Contract" only before a role: "Contract Analyst" is about legal contracts
    r"|^\s*contract\s+(?=(?:senior|sr\b|junior|jr\b|lead|principal|software|back|front|full"
    r"|web|mobile|data|ml\b|machine|ai\b|devops|cloud|platform|python|java|ios|android|qa\b"
    r"|test|engineer|developer|programmer))",
    re.I,
)
_MONTHS = r"(?:\d{1,2}|three|four|six|nine|twelve|eighteen)"
_CONTRACT_DESCRIPTION = re.compile(
    rf"\b{_MONTHS}[\s-]*\+?\s*months?\s+contract\b"
    r"|\bcontract[\s-]+to[\s-]+hire\b|\bc2h\b"
    r"|\bc2c\b|\bcorp[\s-]+to[\s-]+corp\b"
    r"|\bw-?2\s+contract(?:or)?\b"
    r"|\b1099\s+(?:contract(?:ors?|s)?|basis|position|role|only)\b|\bw-?2\s*(?:or|/)\s*1099\b"
    r"|\bindependent\s+contractor\b"
    r"|\bon\s+a\s+contract(?:ual)?\s+basis\b"
    r"|\bcontract\s+(?:role|position|assignment|opportunity|engagement)\b"
    r"|\btemporary\s+(?:role|position|assignment|contract)\b",
    re.I,
)
_INTERNSHIP_TITLE = re.compile(r"\bintern(?:ship)?s?\b", re.I)
_PART_TIME_TITLE = re.compile(r"\bpart[\s-]*time\b", re.I)


def _label(label: str) -> EmploymentType | None:
    return next((kind for kind, pattern in _LABELS if pattern.search(label)), None)


def _rank(labels: Iterable[str]) -> EmploymentType | None:
    kinds = {kind for label in labels if (kind := _label(label)) is not None}
    return next((kind for kind in _PRECEDENCE if kind in kinds), None)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _structured(source: str, raw: Mapping[str, Any], description: str | None) -> list[str]:
    """The labels a source gives in its own fields."""
    if source == "ashby":
        return _strings(raw.get("employmentType"))
    if source == "lever":
        categories = raw.get("categories")
        return _strings(categories.get("commitment")) if isinstance(categories, dict) else []
    if source == "indeed":  # "fulltime, contract"
        return [part for value in _strings(raw.get("job_type")) for part in value.split(",")]
    if source == "workday":
        return _strings(raw.get("timeType"))
    if source == "greenhouse":
        metadata = raw.get("metadata")
        if not isinstance(metadata, list):
            return []
        return [
            label
            for item in metadata
            if isinstance(item, dict) and _GREENHOUSE_FIELD.search(str(item.get("name") or ""))
            for label in _strings(item.get("value"))
        ]
    if source == "linkedin" and description:
        return _LINKEDIN_CRITERIA.findall(description)
    return []


def employment_type(
    source: str, raw: Mapping[str, Any], title: str, description: str | None
) -> EmploymentType:
    """The posting's employment type; full time when nothing says otherwise."""
    structured = _rank(_structured(source, raw, description))
    if structured is not None:
        return structured
    if _CONTRACT_TITLE.search(title):
        return "contract"
    if description and _CONTRACT_DESCRIPTION.search(description):
        return "contract"
    if _INTERNSHIP_TITLE.search(title):
        return "internship"
    if _PART_TIME_TITLE.search(title):
        return "part_time"
    return "full_time"


def posting_employment_type(posting: Posting) -> EmploymentType:
    return employment_type(posting.source, posting.raw, posting.title, posting.description_text)
