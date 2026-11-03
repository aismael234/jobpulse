"""Find the ATS board behind a URL, and check it with one API call before anyone adds it.

Detection tries the URL itself first (every Greenhouse, Lever and Ashby form, see
``core.atsurls``), then fetches the page once and looks for board links or the Greenhouse
embed script. Validation calls the board's list endpoint once; a Lever board that 404s on
the US host is tried once more on the EU host, and a Workday board answers the first page of
its job search.
"""

import ipaddress
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from jobpulse.core.atsurls import (
    AtsRef,
    detect_ats_url,
    find_ats_refs_in_html,
    gh_jid,
    workday_board,
)
from jobpulse.core.models import ErrorKind
from jobpulse.httpclient import HttpClient
from jobpulse.sources.ashby import AshbyAdapter
from jobpulse.sources.base import FetchError, HttpAdapter
from jobpulse.sources.greenhouse import GreenhouseAdapter
from jobpulse.sources.lever import LeverAdapter
from jobpulse.sources.workday import WorkdayAdapter

MAX_PAGE_BYTES = 2_000_000
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


@dataclass(frozen=True, slots=True)
class Validation:
    ref: AtsRef  # with the region that answered
    ok: bool
    name: str | None = None
    job_count: int = 0
    error: str | None = None
    error_kind: ErrorKind | None = None
    titles: tuple[str, ...] = ()  # Workday: the first page's titles

    @property
    def permanent(self) -> bool:
        """A failure that retrying won't fix (the board doesn't exist or isn't readable)."""
        return not self.ok and self.error_kind in ("http_4xx", "parse")


def is_fetchable_url(url: str) -> bool:
    """http(s) on a public host name: no localhost, no private or link-local addresses."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https") or not host:
        return False
    if host == "localhost" or host.endswith(_PRIVATE_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." in host  # a bare name like "intranet" only resolves on a local network
    return address.is_global


def _jobs(payload: Any) -> list[Any] | None:
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    return jobs if isinstance(jobs, list) else None


def _postings(payload: Any) -> list[Any] | None:
    return payload if isinstance(payload, list) else None


def _greenhouse_name(jobs: list[Any]) -> str | None:
    for job in jobs:
        name = job.get("company_name") if isinstance(job, dict) else None
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


class AtsDetector:
    def __init__(self, http: HttpClient, *, max_page_bytes: int = MAX_PAGE_BYTES) -> None:
        self._http = http
        self._json = HttpAdapter(http)
        self._max_page_bytes = max_page_bytes

    async def detect(self, url: str, *, fetch_page: bool = True) -> AtsRef | None:
        """The board a URL belongs to: from the URL alone, else from the page it serves."""
        ref = detect_ats_url(url)
        if ref is not None or not fetch_page:
            return ref
        refs = await self.refs_in_page(url)
        if not refs:
            return None
        if gh_jid(url):  # the link names a Greenhouse job, so its board is the answer
            return next((found for found in refs if found.ats == "greenhouse"), refs[0])
        return refs[0]

    async def refs_in_page(self, url: str) -> list[AtsRef] | None:
        """Boards linked or embedded in the page at ``url``; ``None`` if it couldn't be read."""
        if not is_fetchable_url(url):
            return None
        try:
            response = await self._http.get_limited(url, max_bytes=self._max_page_bytes)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        return find_ats_refs_in_html(response.text)

    async def validate(self, ref: AtsRef) -> Validation:
        board = AtsRef(ref.ats, ref.token, None, ref.region)
        if ref.ats == "workday":
            return await self._check_workday(board)
        if ref.ats == "greenhouse":
            validation, _ = await self._check(
                board, GreenhouseAdapter.list_url(ref.token), _jobs, _greenhouse_name
            )
            return validation
        if ref.ats == "lever":
            validation, status = await self._check(
                board, LeverAdapter.list_url(ref.token, ref.region), _postings
            )
            if status == 404 and ref.region == "us":
                validation, _ = await self._check(
                    AtsRef("lever", ref.token, None, "eu"),
                    LeverAdapter.list_url(ref.token, "eu"),
                    _postings,
                )
            return validation
        validation, _ = await self._check(board, AshbyAdapter.list_url(ref.token), _jobs)
        return validation

    async def workday_search(self, ref: AtsRef, keyword: str) -> Validation:
        """How many jobs a keyword finds on a Workday board, from the first page of that
        search (``job_count``)."""
        return await self._check_workday(AtsRef(ref.ats, ref.token, None, ref.region), keyword)

    async def _check_workday(self, ref: AtsRef, keyword: str = "") -> Validation:
        """The first page of the board's job search: Workday counts its jobs there."""
        board = workday_board(ref.token)
        if board is None:
            return Validation(ref, ok=False, error="not a Workday board", error_kind="parse")
        try:
            payload = await self._json.post_json(
                WorkdayAdapter.jobs_url(board), WorkdayAdapter.search_body(keyword, 0)
            )
        except FetchError as exc:
            return Validation(ref, ok=False, error=exc.message, error_kind=exc.kind)
        jobs = payload.get("jobPostings") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return Validation(ref, ok=False, error="unexpected payload", error_kind="parse")
        total = payload.get("total")
        count = total if isinstance(total, int) and total >= len(jobs) else len(jobs)
        titles = tuple(
            job["title"]
            for job in jobs
            if isinstance(job, dict) and isinstance(job.get("title"), str)
        )
        return Validation(ref, ok=True, job_count=count, titles=titles)

    async def _check(
        self,
        ref: AtsRef,
        url: str,
        jobs_of: Callable[[Any], list[Any] | None],
        name_of: Callable[[list[Any]], str | None] = lambda jobs: None,
    ) -> tuple[Validation, int | None]:
        try:
            # Cached, so a first poll of the board right after this can answer with a 304.
            payload = await self._json.get_json(url, use_etag_cache=True)
        except FetchError as exc:
            return Validation(ref, ok=False, error=exc.message, error_kind=exc.kind), exc.status
        jobs = jobs_of(payload)
        if jobs is None:
            return (
                Validation(ref, ok=False, error="unexpected payload", error_kind="parse"),
                200,
            )
        return Validation(ref, ok=True, name=name_of(jobs), job_count=len(jobs)), 200
