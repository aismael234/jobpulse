"""Normalized domain types shared by the sources, the detection engine and the notifiers."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from jobpulse.core.applicants import ApplicantCount

SourceName = Literal["greenhouse", "lever", "ashby", "simplify", "indeed", "linkedin", "workday"]
AtsName = Literal["greenhouse", "lever", "ashby", "workday"]
SearchSite = Literal["indeed", "linkedin"]
Precision = Literal["exact", "day", "none"]
ErrorKind = Literal[
    "http_429", "http_4xx", "http_5xx", "timeout", "parse", "jobspy_board_error", "other"
]
Tier = Literal["pending", "baseline", "merged", "filtered", "stale", "instant"]
Region = Literal["us", "eu"]

FeedName = Literal["simplify"]

ATS_SOURCES: frozenset[str] = frozenset({"greenhouse", "lever", "ashby", "workday"})
JOB_BOARD_SOURCES: frozenset[str] = frozenset({"indeed", "linkedin"})
FEED_SOURCES: frozenset[str] = frozenset({"simplify"})
# New postings from these sources go through cross-source dedupe (detection rule 5).
DEDUPE_SOURCES: frozenset[str] = frozenset({"indeed", "linkedin", "simplify"})


@dataclass(frozen=True, slots=True, kw_only=True)
class Posting:
    """One job posting, normalized. ``canonical_url`` is an ATS key or a cleaned URL."""

    source: SourceName
    external_id: str
    company_name: str
    company_token: str | None
    title: str
    locations: tuple[str, ...]
    is_remote: bool | None
    workplace_type: str | None
    url: str
    apply_url: str | None
    canonical_url: str
    posted_at: datetime | None
    posted_at_precision: Precision
    description_text: str | None = None
    department: str | None = None
    compensation: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    applicants: ApplicantCount | None = None  # LinkedIn's applicant line, from its job page

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("external_id must not be empty")
        if self.posted_at is not None and self.posted_at.utcoffset() is None:
            raise ValueError("posted_at must be timezone-aware")
        if (self.posted_at is None) != (self.posted_at_precision == "none"):
            raise ValueError("posted_at_precision must be 'none' exactly when posted_at is None")


@dataclass(frozen=True, slots=True, kw_only=True)
class CompanyTarget:
    """A watchlist company whose ATS board returns its complete list of open jobs (a Workday
    board searched by ``keywords`` returns only the matches)."""

    company_id: int
    ats: AtsName
    token: str
    name: str
    region: Region = "us"
    priority: int = 0
    baseline_done: bool = False
    keywords: tuple[str, ...] = ()

    @property
    def source(self) -> str:
        return self.ats

    @property
    def key(self) -> str:
        return self.token


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchTarget:
    """A job-board search. Its results are a window, never a complete listing."""

    search_id: int
    site: SearchSite
    name: str
    search_term: str
    location: str | None = None
    hours_old: int = 3
    params: dict[str, Any] = field(default_factory=dict)
    baseline_done: bool = False

    @property
    def source(self) -> str:
        return self.site

    @property
    def key(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True, kw_only=True)
class FeedTarget:
    """A public feed such as the SimplifyJobs list. It carries closed listings too, so its
    closures come from the feed's own flags, never from absence. ``etag`` is the validator
    saved after the last successful fetch."""

    feed: FeedName
    name: str
    url: str
    etag: str | None = None
    baseline_done: bool = False

    @property
    def source(self) -> str:
        return self.feed

    @property
    def key(self) -> str:
        return self.name


Target = CompanyTarget | SearchTarget | FeedTarget


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchResult:
    """What one fetch of one target produced. A failure never carries postings.

    ``closed_external_ids`` are listings the source itself reports closed (Simplify's
    ``active: false``). ``etag`` is saved together with the result. A ``skipped`` result was
    never attempted (a rate-limit back-off), so no run is recorded for it.
    """

    ok: bool
    fetched_at: datetime
    postings: tuple[Posting, ...] = ()
    complete_listing: bool = False
    error: str | None = None
    error_kind: ErrorKind | None = None
    warnings: tuple[str, ...] = ()
    not_modified: bool = False
    closed_external_ids: tuple[str, ...] = ()
    etag: str | None = None
    skipped: bool = False

    def __post_init__(self) -> None:
        if self.ok and (self.error_kind is not None or self.skipped):
            raise ValueError("a successful FetchResult has no error_kind and isn't skipped")
        if not self.ok and (
            self.error_kind is None
            or self.postings
            or self.complete_listing
            or self.closed_external_ids
            or self.not_modified
        ):
            raise ValueError("a failed FetchResult needs an error_kind and carries no postings")
        if self.not_modified and self.postings:
            raise ValueError("a not-modified FetchResult carries no postings")

    @classmethod
    def success(
        cls,
        postings: tuple[Posting, ...] | list[Posting],
        *,
        fetched_at: datetime,
        complete_listing: bool,
        warnings: tuple[str, ...] | list[str] = (),
        closed_external_ids: tuple[str, ...] | list[str] = (),
        etag: str | None = None,
    ) -> "FetchResult":
        return cls(
            ok=True,
            fetched_at=fetched_at,
            postings=tuple(postings),
            complete_listing=complete_listing,
            warnings=tuple(warnings),
            closed_external_ids=tuple(closed_external_ids),
            etag=etag,
        )

    @classmethod
    def unchanged(cls, *, fetched_at: datetime, etag: str | None) -> "FetchResult":
        """A 304: the source has nothing new since ``etag``. A success, not an empty list."""
        return cls(ok=True, fetched_at=fetched_at, not_modified=True, etag=etag)

    @classmethod
    def failure(cls, kind: ErrorKind, error: str, *, fetched_at: datetime) -> "FetchResult":
        return cls(ok=False, fetched_at=fetched_at, error=error, error_kind=kind)

    @classmethod
    def skip(cls, kind: ErrorKind, reason: str, *, fetched_at: datetime) -> "FetchResult":
        return cls(ok=False, fetched_at=fetched_at, error=reason, error_kind=kind, skipped=True)


@dataclass(frozen=True, slots=True, kw_only=True)
class Decision:
    """The detection engine's verdict on a new posting."""

    tier: Tier
    reason: str
    score: float | None = None
    breakdown: dict[str, float | None] | None = None
