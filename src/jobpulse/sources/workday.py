"""Workday's public careers API (CXS), no auth.

``POST https://{tenant}.wdN.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs`` with
``{"appliedFacets": {}, "limit": 20, "offset": N, "searchText": "..."}`` lists jobs. ``limit``
above 20 answers HTTP 400, so it is fixed at 20. ``total`` is right on the first page only
(later pages say 0), so it is read there. Items carry ``title``, ``externalPath``,
``locationsText`` ("2 Locations" when there are several), ``postedOn`` ("Posted Today",
"Posted 30+ Days Ago") and ``bulletFields`` (the requisition id). Results are mostly newest
first, but not strictly, so a partial read can't be trusted to hold the new jobs.

A board with ``keywords`` is searched once per keyword (``searchText``) and the matches
merged; that is never the complete listing, so it never closes jobs. A board without keywords
is paged through, and the listing counts as complete (closures run) only when every page was
read and the jobs found reach the first page's ``total``. ``max_pages`` caps each search.

``GET {api}{externalPath}`` reads one job (``jobPostingInfo``): the description, ``startDate``
(the posting day), the real locations and the remote type. It runs only for new postings
that pass the prescreen.
"""

import dataclasses
import hashlib
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from jobpulse.core.atsurls import WorkdayBoard, workday_board
from jobpulse.core.dedupe import ats_key
from jobpulse.core.locations import normalize_locations
from jobpulse.core.models import CompanyTarget, FetchResult, Posting, Target
from jobpulse.core.text import html_to_text
from jobpulse.core.timeparse import parse_workday_posted_on, utcnow
from jobpulse.httpclient import HttpClient
from jobpulse.sources.base import FetchError, HttpAdapter, require_company
from jobpulse.sources.maintenance import MaintenanceGate

log = logging.getLogger(__name__)

PAGE_LIMIT = 20  # Workday answers 400 above 20, so this is never configurable
DEFAULT_MAX_PAGES = 25
MAX_EXTERNAL_ID_CHARS = 255  # postings.external_id
_SEVERAL_LOCATIONS = re.compile(r"\d+\s+locations?", re.IGNORECASE)
# "US, CA, Santa Clara" and "USA.VA.Reston": country, state, city, as some tenants write them.
_US_STATE_CITY = re.compile(
    r"(?:US|USA|United States(?: of America)?)\s*[,.]\s*(?P<state>[A-Z]{2})\s*[,.]\s*(?P<city>.+)"
)
_WORKPLACE_WORDS = (
    ("remote", "remote"),
    ("hybrid", "hybrid"),
    ("flex", "hybrid"),  # "Flex", "Office - Flexible"
    ("site", "onsite"),  # "On-site", "Onsite"
    ("office", "onsite"),
)
_STATUS_HINTS = {
    404: "no such careers site: check the site part of the token",
    422: "no such tenant on that host: check the tenant and the wdN part of the token",
}


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _workplace(value: Any) -> str | None:
    text = (_text(value) or "").lower()
    return next((kind for word, kind in _WORKPLACE_WORDS if word in text), None)


def _place(text: str) -> str:
    match = _US_STATE_CITY.fullmatch(text)
    return f"{match['city'].strip()}, {match['state']}" if match else text


def _start_day(value: Any) -> datetime | None:
    """``startDate`` from the detail call: the posting day, counted as a UTC day."""
    text = _text(value)
    if text is None:
        return None
    try:
        day = date.fromisoformat(text[:10])
    except ValueError:
        return None
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _external_id(board: WorkdayBoard, job_id: str) -> str:
    """``{tenant}.{host}/{site}/{job}``, lowercased: Workday job ids are unique only within a
    site. Ids too long for the column keep the board and a hash of the job."""
    key = f"{board.token}/{job_id}".lower()
    if len(key) <= MAX_EXTERNAL_ID_CHARS:
        return key
    return f"{board.token.lower()}/{hashlib.sha256(job_id.lower().encode()).hexdigest()[:32]}"


@dataclass
class _Listing:
    items: list[Any]
    total: int
    cut: bool = False  # max_pages ran out before the jobs did


class WorkdayAdapter(HttpAdapter):
    name = "workday"

    def __init__(
        self,
        http: HttpClient,
        *,
        aliases: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = utcnow,
        max_pages: int = DEFAULT_MAX_PAGES,
        maintenance: MaintenanceGate | None = None,
    ) -> None:
        super().__init__(http, aliases=aliases, clock=clock, maintenance=maintenance)
        self.max_pages = max_pages

    @staticmethod
    def jobs_url(board: WorkdayBoard) -> str:
        return f"{board.api_url}/jobs"

    @staticmethod
    def search_body(search_text: str, offset: int) -> dict[str, Any]:
        return {
            "appliedFacets": {},
            "limit": PAGE_LIMIT,
            "offset": offset,
            "searchText": search_text,
        }

    async def _fetch(self, target: Target, fetched_at: datetime) -> FetchResult:
        company = require_company(target)
        board = workday_board(company.token)
        if board is None:
            raise FetchError("other", f"{company.token!r} isn't a Workday board (tenant.wdN/site)")
        found: dict[str, Posting] = {}
        warnings: list[str] = []
        complete = not company.keywords  # keyword matches are never the whole board
        skipped = 0
        for keyword in company.keywords or ("",):
            listing = await self._search(board, keyword)
            for item in listing.items:
                posting = self.parse_job(item, company, fetched_at=fetched_at)
                if posting is None:
                    skipped += 1
                else:
                    found.setdefault(posting.external_id, posting)
            searched = f"keyword {keyword!r}" if keyword else "the board"
            if listing.cut:
                complete = False
                warnings.append(
                    f"{searched}: read {len(listing.items)} of {listing.total} jobs "
                    f"(workday.max_pages is {self.max_pages}); search by keywords instead of "
                    "paging through every job"
                )
            elif len(listing.items) < listing.total:
                complete = False
                warnings.append(
                    f"{searched}: read {len(listing.items)} of {listing.total} jobs; the listing "
                    "changed while it was paged, so closures wait for a full read"
                )
        if skipped:
            complete = False
            warnings.append(f"skipped {skipped} malformed job(s); closures paused this poll")
        return FetchResult.success(
            list(found.values()),
            fetched_at=fetched_at,
            complete_listing=complete,
            warnings=warnings,
        )

    async def _search(self, board: WorkdayBoard, search_text: str) -> _Listing:
        """Every page of one search, up to ``max_pages``. ``total`` comes from the first
        page; later pages report 0."""
        try:
            return await self._pages(board, search_text)
        except FetchError as exc:
            hint = _STATUS_HINTS.get(exc.status or 0)
            if hint is None:
                raise
            raise FetchError(exc.kind, f"{exc.message} ({hint})", status=exc.status) from None

    async def _pages(self, board: WorkdayBoard, search_text: str) -> _Listing:
        url = self.jobs_url(board)
        listing = _Listing(items=[], total=0)
        for page in range(self.max_pages):
            payload = await self.post_json(url, self.search_body(search_text, page * PAGE_LIMIT))
            jobs = payload.get("jobPostings") if isinstance(payload, dict) else None
            if not isinstance(jobs, list):
                raise FetchError("parse", "expected an object with a 'jobPostings' list")
            if page == 0:
                total = payload.get("total")
                listing.total = total if isinstance(total, int) and total > 0 else 0
                if not jobs and listing.total:
                    raise FetchError(
                        "parse",
                        f"the first page is empty although Workday counts {listing.total} jobs",
                    )
            listing.items.extend(jobs)
            if len(jobs) < PAGE_LIMIT or len(listing.items) >= listing.total:
                return listing
        listing.cut = len(listing.items) < listing.total
        return listing

    def parse_job(
        self, item: Any, company: CompanyTarget, *, fetched_at: datetime
    ) -> Posting | None:
        """One ``jobPostings`` item; ``None`` when it's unusable."""
        board = workday_board(company.token)
        if board is None or not isinstance(item, dict):
            return None
        title, path = _text(item.get("title")), _text(item.get("externalPath"))
        if title is None or path is None or not path.startswith("/"):
            return None
        job_id = path.rstrip("/").rsplit("/", 1)[-1]
        if not job_id:
            return None
        locations, located_remote = self._locations([item.get("locationsText")])
        workplace = _workplace(item.get("remoteType"))
        posted_on = _text(item.get("postedOn"))
        posted = parse_workday_posted_on(posted_on, fetched_at) if posted_on else None
        url = f"{board.board_url}{path}"
        return Posting(
            source="workday",
            external_id=_external_id(board, job_id),
            company_name=company.name,
            company_token=company.token,
            title=title,
            locations=locations,
            is_remote=(workplace == "remote") if workplace else located_remote,
            workplace_type=workplace,
            url=url,
            apply_url=url,
            canonical_url=ats_key("workday", board.token, job_id),
            posted_at=posted.at if posted else None,
            posted_at_precision=posted.precision if posted else "none",
            raw=item,
        )

    def _locations(self, values: Iterable[Any]) -> tuple[tuple[str, ...], bool | None]:
        """Normalized places; "2 Locations" says nothing yet (the detail call names them)."""
        places = [
            _place(text)
            for text in (_text(value) for value in values)
            if text is not None and not _SEVERAL_LOCATIONS.fullmatch(text)
        ]
        return normalize_locations(places, self.aliases)

    async def enrich(self, posting: Posting) -> Posting:
        """The description, the posting day, the real locations and the remote type."""
        board = workday_board(posting.company_token or "") if posting.source == "workday" else None
        path = posting.raw.get("externalPath")
        if board is None or not isinstance(path, str) or not path.startswith("/"):
            return posting
        try:
            payload = await self.get_json(f"{board.api_url}{path}")
        except FetchError as exc:
            log.warning("workday detail %s%s failed: %s", board.token, path, exc.message)
            return posting
        info = payload.get("jobPostingInfo") if isinstance(payload, dict) else None
        if not isinstance(info, dict):
            return posting
        description = info.get("jobDescription")
        extra = info.get("additionalLocations")
        locations, located_remote = self._locations(
            [info.get("location"), *(extra if isinstance(extra, list) else [])]
        )
        workplace = _workplace(info.get("remoteType")) or posting.workplace_type
        started = _start_day(info.get("startDate"))
        if not locations:
            locations, located_remote = posting.locations, posting.is_remote
        return dataclasses.replace(
            posting,
            description_text=(html_to_text(description) if isinstance(description, str) else None)
            or posting.description_text,
            locations=locations,
            is_remote=(workplace == "remote") if workplace else located_remote,
            workplace_type=workplace,
            posted_at=started or posting.posted_at,
            posted_at_precision="day" if started else posting.posted_at_precision,
        )
