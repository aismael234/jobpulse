"""LinkedIn's public job page (no login, no cookies): the description and the applicant line
of a LinkedIn posting, from one request.

``GET https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}`` answers with an HTML
fragment. The applicant line ("Be among the first 25 applicants", "87 applicants", "Over 200
applicants") is read from the page's text, not from a DOM path; the description is the page's
description section, its criteria list (seniority, employment type, ...) included.

Pages are read only for LinkedIn postings that pass the prescreen, one at a time through the
shared ``LinkedInGate``, with no retries. A 429 (or LinkedIn's 999, a 403, or a redirect to the
sign-in wall) starts the cool-down, which also ends job pages for the rest of the cycle; the
postings are then judged without their page. At most ``max_per_hour`` pages are read.
"""

import dataclasses
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from jobpulse.core.applicants import ApplicantCount, parse_applicants
from jobpulse.core.models import FetchResult, Posting, Target
from jobpulse.core.text import html_to_text, texts_by_class
from jobpulse.httpclient import HttpClient
from jobpulse.sources.base import SourceAdapter
from jobpulse.sources.linkedin_gate import LinkedInGate

log = logging.getLogger(__name__)

JOB_PAGE_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
MAX_PAGE_BYTES = 1_000_000
DEFAULT_PAGES_PER_HOUR = 40
HOUR_SECONDS = 3600.0
# 999 is LinkedIn's own "request denied"; a 403 here means the same.
_BLOCK_STATUSES = frozenset({403, 429, 999})
# Where LinkedIn sends a guest it won't serve (a closed job may redirect elsewhere).
_SIGN_IN_PATHS = ("/authwall", "/login", "/uas/", "/checkpoint", "/signup")
_DESCRIPTION_CLASS = "show-more-less-html__markup"
_CRITERIA_HEADER_CLASS = "description__job-criteria-subheader"
_CRITERIA_TEXT_CLASS = "description__job-criteria-text"


@dataclass(frozen=True, slots=True)
class JobPage:
    description: str | None = None
    applicants: ApplicantCount | None = None


def parse_job_page(page: str) -> JobPage:
    """The description (with the criteria list) and the applicant line of a job page."""
    body = next((text for text in texts_by_class(page, _DESCRIPTION_CLASS) if text), None)
    criteria = [
        f"{header}: {value}"
        for header, value in zip(
            texts_by_class(page, _CRITERIA_HEADER_CLASS),
            texts_by_class(page, _CRITERIA_TEXT_CLASS),
            strict=False,
        )
        if header and value
    ]
    description = "\n".join(part for part in (body, *criteria) if part) or None
    return JobPage(description, parse_applicants(html_to_text(page)))


class LinkedInJobPages:
    def __init__(
        self,
        http: HttpClient,
        gate: LinkedInGate,
        *,
        max_per_hour: int = DEFAULT_PAGES_PER_HOUR,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._gate = gate
        self._max_per_hour = max_per_hour
        self._monotonic = monotonic
        self._read: deque[float] = deque()  # when each page of the last hour was read

    async def enrich(self, posting: Posting) -> Posting:
        """``posting`` with its page's description and applicant count, or unchanged."""
        if posting.source != "linkedin" or not posting.external_id.isdigit():
            return posting
        if not self._can_read():
            return posting
        async with self._gate.turn():
            if not self._can_read():  # a page read while this one waited may have been refused
                return posting
            self._read.append(self._monotonic())
            page = await self._fetch(posting.external_id)
        if page is None:
            return posting
        return dataclasses.replace(
            posting,
            description_text=page.description or posting.description_text,
            applicants=page.applicants or posting.applicants,
        )

    def _can_read(self) -> bool:
        if self._gate.blocked_until() is not None:
            return False
        now = self._monotonic()
        while self._read and now - self._read[0] >= HOUR_SECONDS:
            self._read.popleft()
        return len(self._read) < self._max_per_hour

    async def _fetch(self, job_id: str) -> JobPage | None:
        """One GET, never retried; ``None`` when the page couldn't be read."""
        try:
            response = await self._http.get_limited(
                JOB_PAGE_URL.format(job_id=job_id), max_bytes=MAX_PAGE_BYTES
            )
        except httpx.HTTPError as exc:
            log.warning("linkedin job page %s: %s", job_id, type(exc).__name__)
            return None
        signed_out = response.request.url.path.startswith(_SIGN_IN_PATHS)
        if response.status_code in _BLOCK_STATUSES or signed_out:
            until = self._gate.back_off()
            reason = "sent to the sign-in wall" if signed_out else f"HTTP {response.status_code}"
            log.warning(
                "LinkedIn refused job page %s (%s); no LinkedIn requests until %s UTC",
                job_id, reason, f"{until:%H:%M}",
            )  # fmt: skip
            return None
        if response.status_code != 200 or not response.request.url.path.startswith("/jobs-guest/"):
            log.info("linkedin job page %s: HTTP %d", job_id, response.status_code)
            return None
        return parse_job_page(response.text)


class LinkedInSource:
    """The ``linkedin`` source: searches through JobSpy, details from each job's page."""

    name = "linkedin"

    def __init__(self, searches: SourceAdapter, pages: LinkedInJobPages) -> None:
        self._searches = searches
        self._pages = pages

    async def fetch(self, target: Target) -> FetchResult:
        return await self._searches.fetch(target)

    async def enrich(self, posting: Posting) -> Posting:
        return await self._pages.enrich(posting)
