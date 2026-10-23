"""Indeed and LinkedIn searches through JobSpy (python-jobspy 1.4.0).

``scrape_jobs`` blocks, so every call runs in a worker thread: one site per call, so each
search gets its own run record and error, and one search at a time. LinkedIn calls are
paced ``linkedin.delay_*_seconds`` apart (JobSpy's own delay between result pages uses the
same range) and stop for ``linkedin.cooldown_minutes`` after a 429. ``proxies`` is never
passed, and Glassdoor, ZipRecruiter and Google are never asked for.

JobSpy doesn't raise for a failed board; it names the board in ``frame.attrs["errors"]``,
sometimes next to rows it did get. Those rows are dropped: the run is a failure, and the
next overlapping window picks them up.
"""

import asyncio
import math
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from jobpulse.core.dedupe import canonicalize_url
from jobpulse.core.jobboards import board_job_id
from jobpulse.core.locations import DEFAULT_ALIASES, normalize_locations
from jobpulse.core.models import ErrorKind, FetchResult, Posting, SearchSite, SearchTarget, Target
from jobpulse.core.timeparse import utcnow

SITES: tuple[SearchSite, ...] = ("indeed", "linkedin")
DEFAULT_RESULTS_WANTED = 30  # the config default; synced searches always carry their own
_ID_PREFIXES: dict[str, str] = {"linkedin": "li-", "indeed": "in-"}
# Long text that is kept once, in description_text, rather than again in raw.
_NOT_IN_RAW = frozenset({"description", "company_description"})
_HTTP_429 = re.compile(r"\b429\b")

Scrape = Callable[..., Any]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Pacing:
    """LinkedIn politeness: a random pause between calls and a cool-down after a 429."""

    delay_min: float = 4.0
    delay_max: float = 8.0
    cooldown: timedelta = timedelta(minutes=30)


DEFAULT_PACING = Pacing()


def jobspy_error_kind(message: str) -> ErrorKind:
    """JobSpy reports a block as "response status code 429 (blocked: use a proxy)"."""
    return "http_429" if _HTTP_429.search(message) else "jobspy_board_error"


def load_scrape_jobs(pacing: Pacing) -> Scrape:
    """Import JobSpy on first use (it pulls in pandas) and give its LinkedIn page delay the
    configured range."""
    from jobspy import scrape_jobs
    from jobspy.linkedin import LinkedIn

    # JobSpy waits uniform(3, 7) s between LinkedIn result pages; use our 4–8 s instead.
    LinkedIn.delay = (pacing.delay_min, pacing.delay_max)  # type: ignore[assignment]
    return scrape_jobs


def _clean(value: Any) -> Any:
    """pandas leaves NaN (or NA, or NaT, depending on the dtype) where JobSpy had None."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if type(value).__name__ in ("NAType", "NaTType"):
        return None
    return value


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, date):  # datetime is a date too
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if hasattr(value, "item"):  # numpy scalars
        return _json_safe(value.item())
    return str(value)


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _amount(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _posted_day(value: Any) -> datetime | None:
    """JobSpy's ``date_posted`` is a date: day precision, counted as a UTC day."""
    if isinstance(value, datetime):
        day = value.astimezone(UTC).date() if value.tzinfo else value.date()
    elif isinstance(value, date):
        day = value
    elif isinstance(value, str):
        try:
            day = date.fromisoformat(value[:10])
        except ValueError:
            return None
    else:
        return None
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


class JobSpyAdapter:
    """One adapter for both sites; the poller registers it as ``indeed`` and ``linkedin``."""

    name = "jobspy"

    def __init__(
        self,
        *,
        scrape: Scrape | None = None,
        aliases: Mapping[str, str] | None = None,
        pacing: Pacing = DEFAULT_PACING,
        clock: Callable[[], datetime] = utcnow,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._scrape = scrape
        self.aliases = aliases if aliases is not None else DEFAULT_ALIASES
        self.pacing = pacing
        self.clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._last_linkedin: float | None = None
        self._linkedin_blocked_until: datetime | None = None

    async def fetch(self, target: Target) -> FetchResult:
        if not isinstance(target, SearchTarget):
            return FetchResult.failure(
                "other",
                f"{type(target).__name__} is not a job-board search target",
                fetched_at=self.clock(),
            )
        linkedin = target.site == "linkedin"
        async with self._lock:  # one search at a time, across both sites
            blocked_until = self._linkedin_blocked_until
            if linkedin and blocked_until is not None and self.clock() < blocked_until:
                return FetchResult.skip(
                    "http_429",
                    f"not attempted: LinkedIn answered 429, backing off until "
                    f"{blocked_until:%H:%M} UTC",
                    fetched_at=self.clock(),
                )
            if linkedin:
                await self._pace()
            fetched_at = self.clock()
            arguments = self.scrape_arguments(target)
            try:
                frame = await asyncio.to_thread(self._call, arguments)
            except Exception as exc:
                message = f"JobSpy raised {type(exc).__name__}: {exc}"
                result = FetchResult.failure(
                    jobspy_error_kind(message), message, fetched_at=fetched_at
                )
            else:
                result = self.parse_frame(frame, target, fetched_at)
            if linkedin:
                self._last_linkedin = self._monotonic()
                if result.error_kind == "http_429":
                    self._linkedin_blocked_until = self.clock() + self.pacing.cooldown
            return result

    async def enrich(self, posting: Posting) -> Posting:
        return posting

    def _call(self, arguments: dict[str, Any]) -> Any:
        if self._scrape is None:
            self._scrape = load_scrape_jobs(self.pacing)
        return self._scrape(**arguments)

    async def _pace(self) -> None:
        if self._last_linkedin is None:
            return
        delay = self._rng.uniform(self.pacing.delay_min, self.pacing.delay_max)
        wait = delay - (self._monotonic() - self._last_linkedin)
        if wait > 0:
            await self._sleep(wait)

    def scrape_arguments(self, search: SearchTarget) -> dict[str, Any]:
        params = search.params
        arguments: dict[str, Any] = {
            "site_name": [search.site],
            "search_term": search.search_term,
            "results_wanted": int(params.get("results_wanted") or DEFAULT_RESULTS_WANTED),
            "hours_old": search.hours_old,
            "country_indeed": str(params.get("country_indeed") or "USA"),
            "fetch_description": False,
            "verbose": 0,
        }
        if search.location:
            arguments["location"] = search.location
        if params.get("is_remote") is not None:
            arguments["is_remote"] = bool(params["is_remote"])
        if params.get("linkedin_company_ids"):
            arguments["linkedin_company_ids"] = [int(i) for i in params["linkedin_company_ids"]]
        return arguments

    def parse_frame(self, frame: Any, search: SearchTarget, fetched_at: datetime) -> FetchResult:
        attrs = getattr(frame, "attrs", None)
        to_dict = getattr(frame, "to_dict", None)
        if not isinstance(attrs, dict) or not callable(to_dict):
            return FetchResult.failure(
                "parse",
                f"JobSpy returned {type(frame).__name__}, not a DataFrame",
                fetched_at=fetched_at,
            )
        errors = attrs.get("errors")
        error = errors.get(search.site) if isinstance(errors, dict) else None
        if error:
            message = f"{search.site}: {error}"
            return FetchResult.failure(
                jobspy_error_kind(str(error)), message, fetched_at=fetched_at
            )
        board_warnings = attrs.get("warnings")
        warnings = [
            f"{site}: {warning}"
            for site, items in (board_warnings.items() if isinstance(board_warnings, dict) else ())
            for warning in items
        ]
        postings: list[Posting] = []
        skipped = mismatched = 0
        for record in to_dict("records"):
            row = {str(key): _clean(value) for key, value in record.items()}
            posting = self._posting(row, search)
            if posting is None:
                skipped += 1
                continue
            jobspy_id = _text(row.get("id"))
            if jobspy_id and jobspy_id != _ID_PREFIXES[search.site] + posting.external_id:
                mismatched += 1
            postings.append(posting)
        if skipped:
            warnings.append(f"skipped {skipped} row(s) without a usable job_url or title")
        if mismatched:
            warnings.append(
                f"{mismatched} row(s) whose JobSpy id disagrees with the id in job_url; "
                "used the job_url id"
            )
        return FetchResult.success(
            postings, fetched_at=fetched_at, complete_listing=False, warnings=warnings
        )

    def _posting(self, row: dict[str, Any], search: SearchTarget) -> Posting | None:
        job_url, title = _text(row.get("job_url")), _text(row.get("title"))
        external_id = board_job_id(search.site, job_url) if job_url else None
        if job_url is None or external_id is None or title is None:
            return None
        direct = _text(row.get("job_url_direct"))
        location = _text(row.get("location"))
        locations, located_remote = normalize_locations(
            [location] if location else [], self.aliases
        )
        flags = [flag for flag in (row.get("is_remote"), located_remote) if isinstance(flag, bool)]
        posted_at = _posted_day(row.get("date_posted"))
        low, high = _amount(row.get("min_amount")), _amount(row.get("max_amount"))
        compensation = (
            {
                "min_amount": low,
                "max_amount": high,
                "currency": _text(row.get("currency")),
                "interval": _text(row.get("interval")),
                "salary_source": _text(row.get("salary_source")),
            }
            if low is not None or high is not None
            else None
        )
        return Posting(
            source=search.site,
            external_id=external_id,
            company_name=_text(row.get("company")) or "",
            company_token=None,
            title=title,
            locations=locations,
            is_remote=any(flags) if flags else None,
            workplace_type=None,
            url=job_url,
            apply_url=direct,
            canonical_url=canonicalize_url(direct or job_url),
            posted_at=posted_at,
            posted_at_precision="day" if posted_at else "none",
            description_text=_text(row.get("description")),
            compensation=compensation,
            raw={key: _json_safe(value) for key, value in row.items() if key not in _NOT_IN_RAW},
        )
