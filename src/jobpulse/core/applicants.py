"""LinkedIn's applicant line ("Be among the first 25 applicants", "87 applicants",
"Over 200 applicants") parsed into a label and a min/max range.

For jobs that apply on the company's own site the count reflects Apply-button clicks, so
treat it as rough. The line comes from LinkedIn's public job page (``sources.linkedin_guest``).
"""

import re
from dataclasses import dataclass

from jobpulse.core.text import collapse_whitespace

_NUMBER = r"\d{1,3}(?:,\d{3})+|\d+"
# One pattern, so the leftmost phrase in a page wins and "Over 200 applicants" is never read
# as an exact "200 applicants".
_APPLICANTS = re.compile(
    rf"\b(?:be\s+among\s+the\s+first\s+(?P<first>{_NUMBER})"
    rf"|over\s+(?P<over>{_NUMBER})"
    rf"|(?P<plus>{_NUMBER})\s*\+"
    rf"|(?P<exact>{_NUMBER}))"
    r"\s+applicants?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ApplicantCount:
    label: str
    minimum: int | None
    maximum: int | None


def _number(text: str) -> int:
    return int(text.replace(",", ""))


def parse_applicants(text: str | None) -> ApplicantCount | None:
    """The first applicant phrase in ``text`` (any surrounding page text is fine)."""
    if not text:
        return None
    match = _APPLICANTS.search(text)
    if match is None:
        return None
    label = collapse_whitespace(match.group(0))
    if match["first"]:
        return ApplicantCount(label, None, _number(match["first"]))
    if match["over"] or match["plus"]:
        return ApplicantCount(label, _number(match["over"] or match["plus"]), None)
    count = _number(match["exact"])
    return ApplicantCount(label, count, count)
