"""Normalized domain types shared by the sources, the detection engine and the notifiers."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

SourceName = Literal["greenhouse", "lever", "ashby", "simplify", "indeed", "linkedin", "workday"]
AtsName = Literal["greenhouse", "lever", "ashby", "workday"]
SearchSite = Literal["indeed", "linkedin"]
Precision = Literal["exact", "day", "none"]
ErrorKind = Literal[
    "http_429", "http_4xx", "http_5xx", "timeout", "parse", "jobspy_board_error", "other"
]
Tier = Literal["pending", "baseline", "merged", "filtered", "stale", "digest", "instant"]
Region = Literal["us", "eu"]

ATS_SOURCES: frozenset[str] = frozenset({"greenhouse", "lever", "ashby", "workday"})
JOB_BOARD_SOURCES: frozenset[str] = frozenset({"indeed", "linkedin"})
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

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("external_id must not be empty")
        if self.posted_at is not None and self.posted_at.utcoffset() is None:
            raise ValueError("posted_at must be timezone-aware")
        if (self.posted_at is None) != (self.posted_at_precision == "none"):
            raise ValueError("posted_at_precision must be 'none' exactly when posted_at is None")


@dataclass(frozen=True, slots=True, kw_only=True)
class CompanyTarget:
    """A watchlist company whose ATS board returns its complete list of open jobs."""

    company_id: int
    ats: AtsName
    token: str
    name: str
    region: Region = "us"
    priority: int = 0
    baseline_done: bool = False

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


Target = CompanyTarget | SearchTarget


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchResult:
    """What one fetch of one target produced. A failure never carries postings."""

    ok: bool
    fetched_at: datetime
    postings: tuple[Posting, ...] = ()
    complete_listing: bool = False
    error: str | None = None
    error_kind: ErrorKind | None = None
    warnings: tuple[str, ...] = ()
    not_modified: bool = False

    def __post_init__(self) -> None:
        if self.ok and self.error_kind is not None:
            raise ValueError("a successful FetchResult has no error_kind")
        if not self.ok and (self.error_kind is None or self.postings or self.complete_listing):
            raise ValueError("a failed FetchResult needs an error_kind and carries no postings")

    @classmethod
    def success(
        cls,
        postings: tuple[Posting, ...] | list[Posting],
        *,
        fetched_at: datetime,
        complete_listing: bool,
        warnings: tuple[str, ...] | list[str] = (),
    ) -> "FetchResult":
        return cls(
            ok=True,
            fetched_at=fetched_at,
            postings=tuple(postings),
            complete_listing=complete_listing,
            warnings=tuple(warnings),
        )

    @classmethod
    def failure(cls, kind: ErrorKind, error: str, *, fetched_at: datetime) -> "FetchResult":
        return cls(ok=False, fetched_at=fetched_at, error=error, error_kind=kind)


@dataclass(frozen=True, slots=True, kw_only=True)
class Decision:
    """The detection engine's verdict on a new posting."""

    tier: Tier
    reason: str
    score: float | None = None
    breakdown: dict[str, float | None] | None = None
