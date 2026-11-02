"""Add the boards behind a file's job links to the watchlist (``jobpulse companies import``).

Any file works. In JSON, every string value that is a URL counts, and a company name in the
same object (or an enclosing one) names its board; URLs inside longer text, such as a job
description, don't count. In other text, every URL counts.

A link counts when it names a Greenhouse, Lever, Ashby or Workday board by itself, or is a
``gh_jid`` link on a company's own site, whose page is read once per host to find the
Greenhouse board. LinkedIn and Indeed links are left to your searches, and other links are
never fetched. Each board not yet on the watchlist is checked like a pasted URL: the
blocklist (config.yaml's and the dashboard's) first, then one validation call. Boards bigger
than ``discovery.max_board_jobs`` are left out, as discovery leaves them.

Workday boards are searched with the keywords you give. Each keyword is searched once here
and dropped for that board when it finds more jobs than a poll reads (``workday.max_pages``
pages): some sites list keyword results by relevance, not date, so new jobs past the last page
read would never be seen. Workday's search is fuzzy too ("full stack" finds a bank's "Full
Time" jobs), so a keyword that costs more than one page a poll is kept only when a title on
its first page passes your title rules. A Workday board with no keyword left is left out.

Added boards have ``source_of_discovery=import``; like any new board, their first poll
stores the jobs they have without notifying you.
"""

import asyncio
import json
import math
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from sqlalchemy.orm import Session, sessionmaker

from jobpulse.config import AppConfig, company_blocklist, filter_rules
from jobpulse.core.atsurls import (
    AtsRef,
    default_board_name,
    detect_ats_url,
    detect_workday_url,
    gh_jid,
)
from jobpulse.core.filters import Blocklist, FilterRules, check_title
from jobpulse.core.jobboards import indeed_job_key, linkedin_job_id
from jobpulse.core.timeparse import utcnow
from jobpulse.db import repo
from jobpulse.db.session import session_scope
from jobpulse.sources.ats_detect import AtsDetector, is_fetchable_url
from jobpulse.sources.workday import PAGE_LIMIT

SOURCE = "import"  # companies.source_of_discovery of the boards added here
CONCURRENCY = 8  # validations at once (per-host limits still apply)
_URL = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}"
_NAME_KEYS = ("company_name", "company", "companyName", "employer", "organization")

Outcome = Literal[
    "added", "would add", "watched", "blocked", "invalid", "too big", "too broad", "needs keywords"
]


@dataclass(frozen=True, slots=True)
class Link:
    url: str
    company: str | None = None  # the name beside it in the file, if any


@dataclass(frozen=True, slots=True)
class BoardResult:
    ref: AtsRef
    outcome: Outcome
    name: str = ""
    link: str = ""  # the first link that named the board
    job_count: int = 0
    keywords: tuple[tuple[str, int], ...] = ()  # Workday: kept keywords and what they find
    dropped: tuple[tuple[str, int], ...] = ()  # Workday: keywords that find too much
    unmatched: tuple[tuple[str, int], ...] = ()  # Workday: keywords that find nothing you want
    detail: str = ""

    @property
    def label(self) -> str:
        return f"{self.ref.ats}/{self.ref.token}"


@dataclass
class ImportReport:
    links: int = 0  # distinct URLs read
    board_links: int = 0  # URLs that name a board, or a gh_jid page
    job_board_links: int = 0  # LinkedIn and Indeed
    other_links: int = 0
    unresolved_pages: int = 0  # gh_jid pages without a Greenhouse board found
    boards: list[BoardResult] = field(default_factory=list)


# --- reading links ----------------------------------------------------------------------------


def _company(value: dict[str, Any], inherited: str | None) -> str | None:
    for key in _NAME_KEYS:
        name = value.get(key)
        if isinstance(name, str) and name.strip():
            return " ".join(name.split())
    return inherited


def _json_links(value: Any, company: str | None = None) -> Iterator[Link]:
    if isinstance(value, dict):
        name = _company(value, company)
        for item in value.values():
            yield from _json_links(item, name)
    elif isinstance(value, list):
        for item in value:
            yield from _json_links(item, company)
    elif isinstance(value, str) and _URL.fullmatch(value.strip()):
        yield Link(value.strip(), company)


def read_links(text: str) -> list[Link]:
    """Distinct links in order of first appearance, each with the first company name given
    for it."""
    try:
        found = list(_json_links(json.loads(text)))
    except ValueError:
        found = [Link(match.group(0).rstrip(_TRAILING)) for match in _URL.finditer(text)]
    links: dict[str, Link] = {}
    for link in found:
        if link.url not in links or (links[link.url].company is None and link.company):
            links[link.url] = link
    return list(links.values())


# --- checking and adding ----------------------------------------------------------------------


def _counts(values: Sequence[tuple[str, int]]) -> str:
    return ", ".join(f"{keyword} ({count:,})" for keyword, count in values)


@dataclass(frozen=True, slots=True)
class KeywordCheck:
    """What each keyword finds on a Workday board, sorted into kept and left out."""

    kept: tuple[tuple[str, int], ...] = ()
    dropped: tuple[tuple[str, int], ...] = ()  # more jobs than a poll reads
    unmatched: tuple[tuple[str, int], ...] = ()  # pages of jobs, none passing the title rules
    error: str | None = None  # a search failed


async def check_workday_keywords(
    detector: AtsDetector,
    ref: AtsRef,
    keywords: Sequence[str],
    *,
    cap: int,
    rules: FilterRules,
) -> KeywordCheck:
    """Search each keyword once: one finding more than ``cap`` jobs (what a poll reads) is
    dropped, and one costing more than a page is kept only when a title on its first page
    passes ``rules``."""
    kept: list[tuple[str, int]] = []
    dropped: list[tuple[str, int]] = []
    unmatched: list[tuple[str, int]] = []
    for keyword in keywords:
        search = await detector.workday_search(ref, keyword)
        if not search.ok:
            return KeywordCheck(error=search.error or "the search failed")
        found = (keyword, search.job_count)
        if search.job_count > cap:
            dropped.append(found)
        elif search.job_count > PAGE_LIMIT and not any(
            check_title(title, rules).passed for title in search.titles
        ):
            unmatched.append(found)  # pages of noise every poll
        else:
            kept.append(found)
    return KeywordCheck(tuple(kept), tuple(dropped), tuple(unmatched))


async def _check(
    ref: AtsRef,
    link: Link,
    *,
    detector: AtsDetector,
    blocklist: Blocklist,
    config: AppConfig,
    rules: FilterRules,
    keywords: Sequence[str],
) -> BoardResult:
    validation = await detector.validate(ref)
    if not validation.ok:
        return BoardResult(ref, "invalid", link=link.url, detail=validation.error or "")
    ref = validation.ref  # with the region that answered (Lever EU)
    name = validation.name or link.company or default_board_name(ref)
    for blocked in (name, link.company):
        if blocked and blocklist.blocks_company(blocked):
            return BoardResult(ref, "blocked", name, link.url, detail=f"{blocked} is blocked")
    count = validation.job_count
    if ref.ats != "workday":
        limit = config.discovery.max_board_jobs
        if limit is not None and count > limit:
            return BoardResult(
                ref,
                "too big",
                name,
                link.url,
                count,
                detail=f"{count:,} open jobs, more than discovery.max_board_jobs ({limit:,})",
            )
        return BoardResult(ref, "would add", name, link.url, count)
    check = await check_workday_keywords(
        detector, ref, keywords, cap=config.workday.max_pages * PAGE_LIMIT, rules=rules
    )
    if check.error is not None:
        return BoardResult(ref, "invalid", name, link.url, count, detail=check.error)
    if not check.kept:
        return BoardResult(
            ref,
            "too broad",
            name,
            link.url,
            count,
            dropped=check.dropped,
            unmatched=check.unmatched,
            detail="no keyword fits",
        )
    return BoardResult(
        ref, "would add", name, link.url, count, check.kept, check.dropped, check.unmatched
    )


async def import_boards(
    links: Sequence[Link],
    *,
    detector: AtsDetector,
    sessions: sessionmaker[Session],
    config: AppConfig,
    keywords: Sequence[str] = (),
    priority: int = 0,
    dry_run: bool = False,
    clock: Callable[[], datetime] = utcnow,
) -> ImportReport:
    """Check the boards behind ``links`` and add the good ones (none in a dry run)."""
    report = ImportReport(links=len(links))
    boards: dict[tuple[str, str], tuple[AtsRef, Link]] = {}
    pages: dict[str, Link] = {}  # host -> its first gh_jid link
    for link in links:
        ref = detect_ats_url(link.url) or detect_workday_url(link.url)
        if ref is not None:
            report.board_links += 1
            board = AtsRef(ref.ats, ref.token, None, ref.region)
            boards.setdefault((ref.ats, ref.token.lower()), (board, link))
        elif gh_jid(link.url) and is_fetchable_url(link.url):
            report.board_links += 1
            pages.setdefault((urlsplit(link.url).hostname or "").lower(), link)
        elif linkedin_job_id(link.url) or indeed_job_key(link.url):
            report.job_board_links += 1
        else:
            report.other_links += 1

    with sessions() as session:
        known = repo.known_companies(session)
        blocklist = repo.blocklist(
            session, company_blocklist(config), agencies=config.hide_agencies
        )

    for link in pages.values():
        refs = await detector.refs_in_page(link.url)
        found = next((ref for ref in refs or () if ref.ats == "greenhouse"), None)
        if found is None:
            report.unresolved_pages += 1
        else:
            boards.setdefault((found.ats, found.token.lower()), (found, link))

    pending: list[tuple[AtsRef, Link]] = []
    for key, (ref, link) in boards.items():
        if key in known:
            report.boards.append(BoardResult(ref, "watched", link=link.url))
        elif blocklist.blocks_board(ref.ats, ref.token):
            report.boards.append(BoardResult(ref, "blocked", link=link.url, detail="board blocked"))
        elif ref.ats == "workday" and not keywords:
            report.boards.append(BoardResult(ref, "needs keywords", link=link.url))
        else:
            pending.append((ref, link))

    gate = asyncio.Semaphore(CONCURRENCY)
    rules = filter_rules(config)

    async def check(ref: AtsRef, link: Link) -> BoardResult:
        async with gate:
            return await _check(
                ref,
                link,
                detector=detector,
                blocklist=blocklist,
                config=config,
                rules=rules,
                keywords=keywords,
            )

    checked = await asyncio.gather(*(check(ref, link) for ref, link in pending))
    if dry_run:
        report.boards += checked
        return report
    with session_scope(sessions) as session:
        for result in checked:
            if result.outcome != "would add":
                report.boards.append(result)
                continue
            added = repo.add_company(
                session,
                ats=result.ref.ats,
                token=result.ref.token,
                region=result.ref.region,
                name=result.name,
                source=SOURCE,
                priority=priority,
                discovered_from=result.link,
                now=clock(),
                params=(
                    {"keywords": [keyword for keyword, _ in result.keywords]}
                    if result.ref.ats == "workday"
                    else None
                ),
            )
            report.boards.append(replace(result, outcome="added" if added else "watched"))
    return report


# --- the report -------------------------------------------------------------------------------

_ATS_NAMES = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workday": "Workday"}


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count:,} {word if count == 1 else plural or word + 's'}"


def _left_out(result: BoardResult, cap: int) -> str:
    """Why keywords were dropped for a Workday board."""
    reasons = []
    if result.dropped:
        reasons.append(f"{_counts(result.dropped)}: more than the {cap:,} jobs a poll reads")
    if result.unmatched:
        reasons.append(
            f"{_counts(result.unmatched)}: nothing on its first page passes your title rules"
        )
    return "; ".join(reasons)


def _line(result: BoardResult, cap: int) -> str:
    label = result.label
    if result.outcome in ("added", "would add"):
        what = (
            f"searching {_counts(result.keywords)}"
            if result.ref.ats == "workday"
            else f"{_plural(result.job_count, 'open job')}"
        )
        line = f"{result.outcome} {label}: {result.name}, {what}"
        if result.dropped or result.unmatched:
            line += f"; left out {_left_out(result, cap)}"
        return line
    if result.outcome == "watched":
        return f"already on the watchlist: {label}"
    if result.outcome == "needs keywords":
        return f"left out {label}: a Workday board needs --workday-keywords"
    if result.outcome == "too broad":
        return (
            f"left out {label} ({result.name}): no keyword fits ({_left_out(result, cap)}); "
            "add it on the Watchlist page with narrower keywords"
        )
    if result.outcome == "too big":
        return (
            f"left out {label} ({result.name}): {result.detail}; add it on the Watchlist page "
            "if you want it anyway"
        )
    if result.outcome == "blocked":
        return f"blocked {label}: {result.detail}"
    return f"couldn't validate {label}: {result.detail}"


_ORDER: dict[str, int] = {
    "added": 0,
    "would add": 0,
    "watched": 1,
    "too broad": 2,
    "too big": 2,
    "needs keywords": 2,
    "blocked": 3,
    "invalid": 4,
}


def render_import(report: ImportReport, *, config: AppConfig) -> list[str]:
    """One line per board (added first), then a summary."""
    cap = config.workday.max_pages * PAGE_LIMIT
    results = sorted(report.boards, key=lambda r: (_ORDER[r.outcome], r.ref.ats, r.ref.token))
    lines = [_line(result, cap) for result in results]
    lines.append(
        f"{_plural(report.links, 'link')} read: {report.board_links:,} name a board "
        f"({_plural(len(report.boards), 'board')}), "
        f"{_plural(report.job_board_links, 'job-board link')} (LinkedIn, Indeed: your searches "
        f"cover those), {report.other_links:,} other (company sites, images, systems jobpulse "
        "can't read)."
    )
    if report.unresolved_pages:
        lines.append(
            f"{_plural(report.unresolved_pages, 'company page')} named no Greenhouse board."
        )
    added = [r for r in report.boards if r.outcome in ("added", "would add")]
    by_ats = {ats: sum(r.ref.ats == ats for r in added) for ats in _ATS_NAMES}
    kinds = ", ".join(f"{count} {_ATS_NAMES[ats]}" for ats, count in by_ats.items() if count)
    verb = "Would add" if any(r.outcome == "would add" for r in added) else "Added"
    summary = f"{verb} {_plural(len(added), 'board')}" + (f" ({kinds})" if kinds else "") + "."
    if added and verb == "Added":
        summary += " Their first poll stores the jobs they have now without notifying you."
    lines.append(summary)
    pages = sum(max(1, math.ceil(count / PAGE_LIMIT)) for r in added for _, count in r.keywords)
    if pages:
        searches = sum(len(r.keywords) for r in added)
        lines.append(
            f"Workday: {_plural(searches, 'keyword search', 'keyword searches')}, about "
            f"{_plural(pages, 'request')} a watchlist poll."
        )
    return lines
