"""Automatic watchlist discovery: every URL a feed or job-board posting carries is checked for
a Greenhouse, Lever or Ashby board. A board not on the watchlist is validated with one API
call and added with ``source_of_discovery=auto`` and priority 0; its first poll is a silent
baseline like any new company's.

A ``gh_jid`` link on a custom domain names a Greenhouse job but not its board, so that page
is fetched once per host to find the token. Other non-ATS links are never fetched. Page
fetches and validations are capped per cycle, best candidates first (boards with a
matching posting, then the most referenced), and misses are remembered for a while so they
aren't retried every cycle.

Narrowing (``discovery`` in config.yaml): with ``require_match`` a board is considered only
when one of its postings passes your title and location filters; boards with more than
``max_board_jobs`` open jobs are skipped; and no more than ``max_auto_companies``
discovered companies are polled at once. Blocked companies and boards are never added.
"""

import asyncio
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from jobpulse.core.atsurls import AtsRef, detect_ats_url, gh_jid
from jobpulse.core.timeparse import utcnow
from jobpulse.sources.ats_detect import AtsDetector, Validation, is_fetchable_url

PERMANENT_MISS_TTL = timedelta(days=7)
TRANSIENT_MISS_TTL = timedelta(hours=2)

CacheKey = tuple[str, ...]  # ("board", ats, token) or ("page", host)


@dataclass(frozen=True, slots=True)
class Sighting:
    """A URL seen on a posting, with the posting's company and whether its title matched."""

    url: str
    company_name: str
    source: str
    relevant: bool = False


@dataclass(frozen=True, slots=True)
class Candidate:
    ref: AtsRef
    company_name: str
    url: str
    sightings: int = 1
    relevant: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.ref.ats, self.ref.token.lower())


@dataclass(frozen=True, slots=True)
class Plan:
    candidates: tuple[Candidate, ...] = ()  # validate these this cycle, best first
    pages: tuple[Sighting, ...] = ()  # gh_jid pages to fetch this cycle, one per host
    deferred: int = 0  # candidates and pages left for later cycles by the caps
    capped: int = 0  # candidates that max_auto_companies leaves no room for


@dataclass(frozen=True, slots=True)
class Found:
    candidate: Candidate
    validation: Validation


@dataclass
class Outcome:
    valid: list[Found] = field(default_factory=list)
    invalid: list[Found] = field(default_factory=list)
    too_big: list[Found] = field(default_factory=list)
    deferred: int = 0
    capped: int = 0


@dataclass
class _Tally:
    first: int
    ref: AtsRef
    sighting: Sighting
    sightings: int = 0
    relevant: int = 0


def _page_host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _board_key(ats: str, token: str) -> CacheKey:
    return ("board", ats, token.lower())


class Discoverer:
    def __init__(
        self,
        detector: AtsDetector,
        *,
        max_validations: int,
        max_page_fetches: int,
        clock: Callable[[], datetime] = utcnow,
        require_match: bool = False,
        max_board_jobs: int | None = None,
    ) -> None:
        self._require_match = require_match
        self._max_board_jobs = max_board_jobs
        self._detector = detector
        self._max_validations = max_validations
        self._max_page_fetches = max_page_fetches
        self._clock = clock
        self._skip_until: dict[CacheKey, datetime] = {}

    def _skipped(self, key: CacheKey, now: datetime) -> bool:
        until = self._skip_until.get(key)
        if until is None:
            return False
        if now >= until:
            del self._skip_until[key]
            return False
        return True

    def _remember(self, key: CacheKey, *, permanent: bool) -> None:
        ttl = PERMANENT_MISS_TTL if permanent else TRANSIENT_MISS_TTL
        self._skip_until[key] = self._clock() + ttl

    def reject(self, ref: AtsRef) -> None:
        """Leave this board alone for a while (it validated, but someone decided against it)."""
        self._remember(_board_key(ref.ats, ref.token), permanent=True)

    def _budget(self, room: int | None) -> tuple[int, bool]:
        """Validations allowed this cycle, and whether ``room`` (not the cycle cap) set it."""
        if room is None or room >= self._max_validations:
            return self._max_validations, False
        return max(0, room), True

    def plan(
        self,
        sightings: Iterable[Sighting],
        *,
        known: Collection[tuple[str, str]],
        greenhouse_hosts: Collection[str],
        room: int | None = None,
    ) -> Plan:
        """What to look at this cycle. ``known`` holds (ats, lowercase token) for every
        company already stored, active or not, and every blocked board; ``greenhouse_hosts``
        the custom domains whose Greenhouse token is already known. ``room`` is how many more
        companies discovery may add in total (``None``: no limit)."""
        now = self._clock()
        known_keys = {_board_key(ats, token) for ats, token in known}
        mapped = {host.lower().removeprefix("www.") for host in greenhouse_hosts}
        boards: dict[CacheKey, _Tally] = {}
        pages: dict[str, Sighting] = {}
        for index, sighting in enumerate(sightings):
            ref = detect_ats_url(sighting.url)
            if ref is not None:
                key = _board_key(ref.ats, ref.token)
                if key in known_keys or self._skipped(key, now):
                    continue
                tally = boards.setdefault(key, _Tally(index, ref, sighting))
                tally.sightings += 1
                tally.relevant += sighting.relevant
            elif gh_jid(sighting.url) and is_fetchable_url(sighting.url):
                if self._require_match and not sighting.relevant:
                    continue
                host = _page_host(sighting.url)
                if host in mapped or self._skipped(("page", host), now):
                    continue
                current = pages.get(host)
                if current is None or (sighting.relevant and not current.relevant):
                    pages[host] = sighting
        ranked = sorted(boards.values(), key=lambda t: (-t.relevant, -t.sightings, t.first))
        if self._require_match:  # a board earns a place with a posting that passes the filters
            ranked = [tally for tally in ranked if tally.relevant]
        candidates = [
            Candidate(t.ref, t.sighting.company_name, t.sighting.url, t.sightings, t.relevant)
            for t in ranked
        ]
        page_list = sorted(pages.values(), key=lambda sighting: not sighting.relevant)
        budget, room_limited = self._budget(room)
        chosen = candidates[:budget]
        # A page is worth reading only if the board it reveals can be validated this cycle.
        page_room = max(0, min(self._max_page_fetches, budget - len(chosen)))
        chosen_pages = page_list[:page_room]
        left_candidates = len(candidates) - len(chosen)
        left_pages = len(page_list) - len(chosen_pages)
        pages_out_of_room = room_limited and len(chosen) + len(chosen_pages) >= budget
        capped = (left_candidates if room_limited else 0) + (left_pages if pages_out_of_room else 0)
        deferred = left_candidates + left_pages - capped
        return Plan(tuple(chosen), tuple(chosen_pages), deferred, capped)

    async def run(
        self,
        sightings: Iterable[Sighting],
        *,
        known: Collection[tuple[str, str]],
        greenhouse_hosts: Collection[str],
        room: int | None = None,
    ) -> Outcome:
        """Plan, fetch the gh_jid pages, then validate; misses go into the cache. Boards
        bigger than ``max_board_jobs`` come back in ``too_big`` and are remembered too."""
        plan = self.plan(sightings, known=known, greenhouse_hosts=greenhouse_hosts, room=room)
        outcome = Outcome(deferred=plan.deferred, capped=plan.capped)
        candidates = list(plan.candidates)
        queued = {_board_key(ats, token) for ats, token in known}
        queued |= {_board_key(c.ref.ats, c.ref.token) for c in candidates}
        page_of: dict[CacheKey, str] = {}  # board found on a page -> that page's host
        now = self._clock()
        for sighting in plan.pages:
            host = _page_host(sighting.url)
            refs = await self._detector.refs_in_page(sighting.url)
            if refs is None:
                self._remember(("page", host), permanent=False)
                continue
            board = next((ref for ref in refs if ref.ats == "greenhouse"), None)
            if board is None:
                self._remember(("page", host), permanent=True)
                continue
            key = _board_key(board.ats, board.token)
            if key in queued or self._skipped(key, now):
                self._remember(("page", host), permanent=True)
                continue
            queued.add(key)
            page_of[key] = host
            candidates.append(
                Candidate(board, sighting.company_name, sighting.url, 1, sighting.relevant)
            )

        budget, room_limited = self._budget(room)
        to_validate = candidates[:budget]
        if room_limited:
            outcome.capped += len(candidates) - len(to_validate)
        else:
            outcome.deferred += len(candidates) - len(to_validate)
        validations = await asyncio.gather(
            *(self._detector.validate(candidate.ref) for candidate in to_validate)
        )
        for candidate, validation in zip(to_validate, validations, strict=True):
            key = _board_key(candidate.ref.ats, candidate.ref.token)
            if key in page_of:  # resolved: no need to read that page again for a while
                self._remember(("page", page_of[key]), permanent=True)
            found = Found(candidate, validation)
            if not validation.ok:
                outcome.invalid.append(found)
                self._remember(key, permanent=validation.permanent)
            elif self._max_board_jobs is not None and validation.job_count > self._max_board_jobs:
                outcome.too_big.append(found)
                self._remember(key, permanent=True)  # no need to download it every cycle
            else:
                outcome.valid.append(found)
        return outcome
