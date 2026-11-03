"""What a resume says that the wizard can prefill (M8), read without an LLM: years of
experience from its date ranges (education left out, overlaps merged), the catalog skills it
names, and the lines that look like job titles."""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from jobpulse.core.roles import suggest_families
from jobpulse.core.skills import find_skills

_MONTHS = {
    name: index
    for index, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}
_MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_POINT = rf"(?:{_MONTH}\s+\d{{4}}|\d{{1,2}}/\d{{4}}|\d{{4}})"
_NOW = r"(?:present|current|now|today|ongoing)"
_RANGE = re.compile(
    rf"(?P<start>{_POINT})\s*(?:-|–|—|to|until)\s*(?P<end>{_POINT}|{_NOW})", re.IGNORECASE
)
_HEADING = re.compile(r"^[A-Za-z][A-Za-z &/]{2,40}:?$")
_EDUCATION = re.compile(
    r"\b(education|academic|coursework|certifications?|publications?|projects?|volunteer)\b",
    re.IGNORECASE,
)
_WORK = re.compile(r"\b(experience|employment|work history|career)\b", re.IGNORECASE)
_TITLE_WORDS = re.compile(
    r"\b(engineer|developer|scientist|architect|programmer|sde|swe|analyst|lead|intern)\b",
    re.IGNORECASE,
)
MAX_TITLE_CHARS = 120


@dataclass(frozen=True, slots=True)
class ResumeFacts:
    years: int | None  # whole years of work, None when no date range was found
    skills: Counter[str]  # catalog skill key → mentions
    titles: tuple[str, ...]  # lines that read like job titles
    families: tuple[str, ...]  # role families those titles suggest


def _month(text: str, *, end: bool, today: date) -> int | None:
    """A point in time as a month count (year * 12 + month - 1)."""
    text = text.strip().lower().rstrip(".")
    if re.fullmatch(_NOW, text):
        return today.year * 12 + today.month - 1
    if match := re.fullmatch(r"(\d{1,2})/(\d{4})", text):
        number, year = int(match[1]), int(match[2])
        return year * 12 + number - 1 if 1 <= number <= 12 else None
    if re.fullmatch(r"\d{4}", text):
        # A bare year covers the whole year: from January, or through December.
        return int(text) * 12 + (11 if end else 0)
    name, _, year_text = text.partition(" ")
    month = _MONTHS.get(name.rstrip("."))
    year_text = year_text.strip()
    if month is None or not year_text.isdigit():
        return None
    return int(year_text) * 12 + month - 1


def _is_heading(line: str) -> bool:
    return bool(_HEADING.fullmatch(line)) and len(line.split()) <= 4


def _work_lines(lines: Iterable[str]) -> list[str]:
    """The lines outside education-like sections. Text before any heading counts as work."""
    kept: list[str] = []
    skipping = False
    for line in lines:
        if _is_heading(line):
            skipping = bool(_EDUCATION.search(line)) and not _WORK.search(line)
            continue
        if not skipping:
            kept.append(line)
    return kept


def years_of_experience(text: str, *, today: date) -> int | None:
    """Whole years covered by the date ranges outside education, overlaps counted once."""
    spans: list[tuple[int, int]] = []
    limit = today.year * 12 + today.month - 1
    for line in _work_lines(line.strip() for line in text.splitlines()):
        for match in _RANGE.finditer(line):
            start = _month(match["start"], end=False, today=today)
            end = _month(match["end"], end=True, today=today)
            if start is None or end is None or end < start or start > limit:
                continue
            spans.append((start, min(end, limit) + 1))
    if not spans:
        return None
    spans.sort()
    months = 0
    current_start, current_end = spans[0]
    for start, end in spans[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            months += current_end - current_start
            current_start, current_end = start, end
    months += current_end - current_start
    return months // 12


def title_lines(text: str) -> tuple[str, ...]:
    """Short lines with a job-title word in them ("Software Engineer, Acme · 2021–2023")."""
    found: list[str] = []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line or len(line) > MAX_TITLE_CHARS or line.endswith("."):
            continue
        if _TITLE_WORDS.search(line) and line not in found:
            found.append(line)
    return tuple(found)


def read_resume(text: str, *, today: date) -> ResumeFacts:
    titles = title_lines(text)
    return ResumeFacts(
        years=years_of_experience(text, today=today),
        skills=find_skills(text),
        titles=titles,
        families=tuple(suggest_families(list(titles))),
    )
