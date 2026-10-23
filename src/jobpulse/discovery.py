"""Automatic watchlist discovery: every URL a feed or job-board posting carries is checked for
a Greenhouse, Lever or Ashby board. A board not on the watchlist is validated with one API
call and added with ``source_of_discovery=auto`` and priority 0; its first poll is a silent
baseline like any new company's.

A ``gh_jid`` link on a custom domain names a Greenhouse job but not its board, so that page
is fetched once per host to find the token. Other non-ATS links are never fetched. Page
fetches and validations are capped per cycle, best candidates first (boards with a
title-matching posting, then the most referenced), and misses are remembered for a while
so they aren't retried every cycle.
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


@dataclass(frozen=True, slots=True)
class Found:
    candidate: Candidate
    validation: Validation


@dataclass
class Outcome:
    valid: list[Found] = field(default_factory=list)
    invalid: list[Found] = field(default_factory=list)
    deferred: int = 0


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
    ) -> None:
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

    def plan(
        self,
        sightings: Iterable[Sighting],
        *,
        known: Collection[tuple[str, str]],
        greenhouse_hosts: Collection[str],
    ) -> Plan:
        """What to look at this cycle. ``known`` holds (ats, lowercase token) for every
        company already stored, active or not; ``greenhouse_hosts`` the custom domains whose
        Greenhouse token is already known."""
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
                host = _page_host(sighting.url)
                if host in mapped or self._skipped(("page", host), now):
                    continue
                current = pages.get(host)
                if current is None or (sighting.relevant and not current.relevant):
                    pages[host] = sighting
        ranked = sorted(boards.values(), key=lambda t: (-t.relevant, -t.sightings, t.first))
        candidates = [
            Candidate(t.ref, t.sighting.company_name, t.sighting.url, t.sightings, t.relevant)
            for t in ranked
        ]
        page_list = sorted(pages.values(), key=lambda sighting: not sighting.relevant)
        chosen = candidates[: self._max_validations]
        # A page is worth reading only if the board it reveals can be validated this cycle.
        room = min(self._max_page_fetches, self._max_validations - len(chosen))
        chosen_pages = page_list[:room]
        deferred = len(candidates) - len(chosen) + len(page_list) - len(chosen_pages)
        return Plan(tuple(chosen), tuple(chosen_pages), deferred)

    async def run(
        self,
        sightings: Iterable[Sighting],
        *,
        known: Collection[tuple[str, str]],
        greenhouse_hosts: Collection[str],
    ) -> Outcome:
        """Plan, fetch the gh_jid pages, then validate; misses go into the cache."""
        plan = self.plan(sightings, known=known, greenhouse_hosts=greenhouse_hosts)
        outcome = Outcome(deferred=plan.deferred)
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

        to_validate = candidates[: self._max_validations]
        outcome.deferred += len(candidates) - len(to_validate)
        validations = await asyncio.gather(
            *(self._detector.validate(candidate.ref) for candidate in to_validate)
        )
        for candidate, validation in zip(to_validate, validations, strict=True):
            key = _board_key(candidate.ref.ats, candidate.ref.token)
            if key in page_of:  # resolved: no need to read that page again for a while
                self._remember(("page", page_of[key]), permanent=True)
            found = Found(candidate, validation)
            if validation.ok:
                outcome.valid.append(found)
            else:
                outcome.invalid.append(found)
                self._remember(key, permanent=validation.permanent)
        return outcome
