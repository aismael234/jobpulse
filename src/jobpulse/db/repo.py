"""Repository functions over the ORM models.

Everything here is synchronous and leaves the transaction to the caller: the poller commits
between phases (or only flushes, in a dry run), and never awaits while a transaction is open.
"""

from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlsplit

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from jobpulse.config import SearchEntry, SimplifyConfig, WatchlistEntry
from jobpulse.core.applicants import ApplicantCount
from jobpulse.core.assessment import LEVELS, Assessment
from jobpulse.core.atsurls import gh_jid
from jobpulse.core.dedupe import (
    canonicalize_url,
    merge_also_seen,
    normalize_company,
    posting_fingerprint,
)
from jobpulse.core.detection import PresencePlan, PresenceRow
from jobpulse.core.filters import Blocklist
from jobpulse.core.models import (
    ATS_SOURCES,
    DEDUPE_SOURCES,
    AtsName,
    CompanyTarget,
    Decision,
    FeedTarget,
    FetchResult,
    Posting,
    Precision,
    Region,
    SearchSite,
    SearchTarget,
    SourceName,
    Target,
    Tier,
)
from jobpulse.db.tables import (
    BlockedCompany,
    Company,
    HealthIncident,
    LlmAssessment,
    PostingRow,
    Run,
    Search,
    SourceState,
)
from jobpulse.llm.base import LlmReply

CHUNK = 500
MAX_ERROR_CHARS = 2000
_ATS_KEY_PREFIXES = tuple(f"{ats}:" for ats in sorted(ATS_SOURCES))
# When several stored rows match, the ATS row is the primary.
_ATS_ROWS_FIRST = case((PostingRow.source.in_(sorted(ATS_SOURCES)), 0), else_=1)


@dataclass(frozen=True, slots=True)
class CompanyOverview:
    ats: str
    token: str
    name: str
    region: str
    priority: int
    active: bool
    source_of_discovery: str
    baseline_done_at: datetime | None
    open_postings: int
    id: int
    discovered_from: str | None


@dataclass(frozen=True, slots=True)
class SyncReport:
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    deactivated: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Adoption:
    """A new ATS row took over a copy of the same job that a feed or job board had first."""

    primary_id: int
    copy_id: int
    copy_source: str
    how: str


def _chunks[T](items: Sequence[T]) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), CHUNK):
        yield items[start : start + CHUNK]


# --- watchlist ---------------------------------------------------------------------------


def _company_params(entry: WatchlistEntry) -> dict[str, Any] | None:
    return {"keywords": list(entry.keywords)} if entry.keywords else None


def _keywords(params: dict[str, Any] | None) -> tuple[str, ...]:
    words = (params or {}).get("keywords")
    return tuple(str(word) for word in words) if isinstance(words, list) else ()


def sync_watchlist(
    session: Session, entries: Sequence[WatchlistEntry], *, now: datetime
) -> SyncReport:
    """Idempotent upsert of the config watchlist on (ats_type, token).

    Config entries own their rows: a company that came from the dashboard or discovery and
    is then added to config becomes config-owned. Config-owned companies that left config are
    deactivated, never deleted, so their history stays. A Workday board whose keywords
    changed finds a different set of jobs, so it starts a new silent baseline.
    """
    by_key = {(c.ats_type, c.token.lower()): c for c in session.scalars(select(Company))}
    added: list[str] = []
    updated: list[str] = []
    configured: set[tuple[str, str]] = set()
    for entry in entries:
        key: tuple[str, str] = (entry.ats, entry.token.lower())
        configured.add(key)
        label = f"{entry.ats}/{entry.token}"
        company = by_key.get(key)
        if company is None:
            session.add(
                Company(
                    name=entry.name,
                    ats_type=entry.ats,
                    token=entry.token,
                    region=entry.region,
                    priority=entry.priority,
                    active=entry.active,
                    source_of_discovery="config",
                    params=_company_params(entry),
                    created_at=now,
                    updated_at=now,
                )
            )
            added.append(label)
            continue
        wanted: dict[str, Any] = {
            "name": entry.name,
            "region": entry.region,
            "priority": entry.priority,
            "active": entry.active,
            "source_of_discovery": "config",
            "params": _company_params(entry),
        }
        if _keywords(company.params) != tuple(entry.keywords):
            company.baseline_done_at = None
        if any(getattr(company, field) != value for field, value in wanted.items()):
            for field, value in wanted.items():
                setattr(company, field, value)
            company.updated_at = now
            updated.append(label)
    deactivated: list[str] = []
    for key, company in by_key.items():
        if key not in configured and company.source_of_discovery == "config" and company.active:
            company.active = False
            company.updated_at = now
            deactivated.append(f"{company.ats_type}/{company.token}")
    session.flush()
    return SyncReport(tuple(added), tuple(updated), tuple(deactivated))


def list_companies(session: Session) -> list[CompanyOverview]:
    open_counts = {
        company_id: count
        for company_id, count in session.execute(
            select(PostingRow.company_id, func.count(PostingRow.id))
            .where(PostingRow.closed_at.is_(None), PostingRow.merged_into_id.is_(None))
            .group_by(PostingRow.company_id)
        )
    }
    companies = session.scalars(select(Company).order_by(Company.ats_type, Company.token))
    return [
        CompanyOverview(
            ats=c.ats_type,
            token=c.token,
            name=c.name,
            region=c.region,
            priority=c.priority,
            active=c.active,
            source_of_discovery=c.source_of_discovery,
            baseline_done_at=c.baseline_done_at,
            open_postings=open_counts.get(c.id, 0),
            id=c.id,
            discovered_from=c.discovered_from,
        )
        for c in companies
    ]


def company_targets(
    session: Session, *, ats: Collection[str] | None = None, token: str | None = None
) -> list[CompanyTarget]:
    query = select(Company).where(Company.active.is_(True)).order_by(Company.id)
    if ats is not None:
        query = query.where(Company.ats_type.in_(list(ats)))
    if token is not None:
        query = query.where(func.lower(Company.token) == token.lower())
    return [
        CompanyTarget(
            company_id=c.id,
            ats=cast(AtsName, c.ats_type),
            token=c.token,
            name=c.name,
            region=cast(Region, c.region),
            priority=c.priority,
            baseline_done=c.baseline_done_at is not None,
            keywords=_keywords(c.params),
        )
        for c in session.scalars(query)
    ]


def search_targets(
    session: Session, *, sites: Collection[str] | None = None, name: str | None = None
) -> list[SearchTarget]:
    query = select(Search).where(Search.active.is_(True)).order_by(Search.id)
    if sites is not None:
        query = query.where(Search.site.in_(list(sites)))
    if name is not None:
        query = query.where(Search.name == name)
    return [
        SearchTarget(
            search_id=s.id,
            site=cast(SearchSite, s.site),
            name=s.name,
            search_term=s.search_term,
            location=s.location,
            hours_old=s.hours_old,
            params=dict(s.params or {}),
            baseline_done=s.baseline_done_at is not None,
        )
        for s in session.scalars(query)
    ]


def mark_baseline_done(session: Session, target: Target, *, now: datetime) -> None:
    if isinstance(target, CompanyTarget):
        session.execute(
            update(Company).where(Company.id == target.company_id).values(baseline_done_at=now)
        )
    elif isinstance(target, SearchTarget):
        session.execute(
            update(Search).where(Search.id == target.search_id).values(baseline_done_at=now)
        )
    else:
        session.execute(
            update(SourceState)
            .where(SourceState.source == target.feed, SourceState.target == target.name)
            .values(baseline_done_at=now, updated_at=now)
        )


def _search_params(entry: SearchEntry) -> dict[str, Any]:
    return {
        "results_wanted": entry.results_wanted,
        "is_remote": entry.is_remote,
        "country_indeed": entry.country_indeed,
        "linkedin_company_ids": list(entry.linkedin_company_ids),
    }


def _search_identity(
    site: str, search_term: str, location: str | None, params: dict[str, Any]
) -> tuple[Any, ...]:
    """What decides which jobs a search finds; when it changes, the baseline starts over."""
    return (
        site,
        search_term,
        location,
        params.get("is_remote"),
        params.get("country_indeed"),
        tuple(params.get("linkedin_company_ids") or ()),
    )


def sync_searches(session: Session, entries: Sequence[SearchEntry], *, now: datetime) -> SyncReport:
    """Idempotent upsert of the config searches on their name. A search whose definition
    changed gets a new silent baseline; searches that left config are deactivated."""
    by_name = {search.name: search for search in session.scalars(select(Search))}
    added: list[str] = []
    updated: list[str] = []
    for entry in entries:
        params = _search_params(entry)
        search = by_name.get(entry.name)
        if search is None:
            session.add(
                Search(
                    name=entry.name,
                    site=entry.site,
                    search_term=entry.search_term,
                    location=entry.location,
                    hours_old=entry.hours_old,
                    params=params,
                    active=entry.active,
                    created_at=now,
                )
            )
            added.append(entry.name)
            continue
        before = _search_identity(
            search.site, search.search_term, search.location, dict(search.params or {})
        )
        if before != _search_identity(entry.site, entry.search_term, entry.location, params):
            search.baseline_done_at = None
        wanted: dict[str, Any] = {
            "site": entry.site,
            "search_term": entry.search_term,
            "location": entry.location,
            "hours_old": entry.hours_old,
            "params": params,
            "active": entry.active,
        }
        if any(getattr(search, field) != value for field, value in wanted.items()):
            for field, value in wanted.items():
                setattr(search, field, value)
            updated.append(entry.name)
    configured = {entry.name for entry in entries}
    deactivated: list[str] = []
    for name, search in by_name.items():
        if name not in configured and search.active:
            search.active = False
            deactivated.append(name)
    session.flush()
    return SyncReport(tuple(added), tuple(updated), tuple(deactivated))


def feed_targets(
    session: Session, feed: SimplifyConfig, *, name: str | None = None
) -> list[FeedTarget]:
    """The SimplifyJobs feed with its stored ETag and baseline. A new URL starts over."""
    if name is not None and name != feed.name:
        return []
    state = session.scalars(
        select(SourceState).where(SourceState.source == "simplify", SourceState.target == feed.name)
    ).first()
    if state is None:
        state = SourceState(source="simplify", target=feed.name, url=feed.url)
        session.add(state)
        session.flush()
    elif state.url != feed.url:
        state.url = feed.url
        state.etag = None
        state.baseline_done_at = None
        session.flush()
    return [
        FeedTarget(
            feed="simplify",
            name=state.target,
            url=state.url,
            etag=state.etag,
            baseline_done=state.baseline_done_at is not None,
        )
    ]


def save_feed_etag(
    session: Session, target: FeedTarget, *, etag: str | None, now: datetime
) -> None:
    session.execute(
        update(SourceState)
        .where(SourceState.source == target.feed, SourceState.target == target.name)
        .values(etag=etag, updated_at=now)
    )


def known_companies(session: Session) -> set[tuple[str, str]]:
    """(ats, lowercase token) of every stored company, active or not."""
    return {
        (ats, token.lower())
        for ats, token in session.execute(select(Company.ats_type, Company.token))
    }


def count_polled_auto_companies(session: Session, blocklist: Blocklist) -> int:
    """Active companies that discovery added and the blocklist doesn't stop: the number
    ``discovery.max_auto_companies`` caps."""
    rows = session.execute(
        select(Company.ats_type, Company.token, Company.name).where(
            Company.active.is_(True), Company.source_of_discovery == "auto"
        )
    )
    return sum(
        1
        for ats, token, name in rows
        if not (blocklist.blocks_board(ats, token) or blocklist.blocks_company(name))
    )


def add_company(
    session: Session,
    *,
    ats: str,
    token: str,
    region: str,
    name: str,
    source: str,
    priority: int,
    discovered_from: str | None,
    now: datetime,
    params: dict[str, Any] | None = None,
) -> bool:
    """Add a board from the dashboard or discovery (baseline pending). False if it existed,
    under any capitalization of its token. ``params`` holds a Workday board's keywords."""
    existing = session.scalars(
        select(Company.id).where(
            Company.ats_type == ats, func.lower(Company.token) == token.lower()
        )
    ).first()
    if existing is not None:
        return False
    statement = (
        _dialect_insert(session)(Company)
        .values(
            name=name,
            ats_type=ats,
            token=token,
            region=region,
            priority=priority,
            active=True,
            source_of_discovery=source,
            discovered_from=discovered_from,
            params=params,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["ats_type", "token"])
        .returning(Company.id)
    )
    return session.execute(statement).scalar_one_or_none() is not None


def add_discovered_company(
    session: Session,
    *,
    ats: str,
    token: str,
    region: str,
    name: str,
    discovered_from: str,
    now: datetime,
) -> bool:
    """Add a board found by discovery (priority 0, baseline pending). False if it existed."""
    return add_company(
        session,
        ats=ats,
        token=token,
        region=region,
        name=name,
        source="auto",
        priority=0,
        discovered_from=discovered_from,
        now=now,
    )


# --- presence and closures --------------------------------------------------------------

_PRESENCE_COLUMNS = (
    PostingRow.id,
    PostingRow.external_id,
    PostingRow.closed_at,
    PostingRow.missing_count,
)


def presence_for_company(
    session: Session, *, source: str, company_id: int
) -> dict[str, PresenceRow]:
    """Every stored posting of one company board, for the closure plan."""
    rows = session.execute(
        select(*_PRESENCE_COLUMNS).where(
            PostingRow.source == source,
            PostingRow.company_id == company_id,
            PostingRow.merged_into_id.is_(None),
        )
    )
    return {row.external_id: PresenceRow(*row) for row in rows}


def presence_for_keys(
    session: Session, *, source: str, external_ids: Iterable[str]
) -> dict[str, PresenceRow]:
    """Stored postings among ``external_ids``: a job-board window is never a full listing."""
    keys = list(dict.fromkeys(external_ids))
    found: dict[str, PresenceRow] = {}
    for start in range(0, len(keys), CHUNK):
        rows = session.execute(
            select(*_PRESENCE_COLUMNS).where(
                PostingRow.source == source,
                PostingRow.external_id.in_(keys[start : start + CHUNK]),
            )
        )
        found.update({row.external_id: PresenceRow(*row) for row in rows})
    return found


def apply_presence(session: Session, plan: PresencePlan, *, now: datetime) -> None:
    for chunk in _chunks(plan.seen_ids):
        session.execute(
            update(PostingRow)
            .where(PostingRow.id.in_(chunk))
            .values(last_seen_at=now, missing_count=0, closed_at=None)
        )
    by_count: dict[int, list[int]] = defaultdict(list)
    for posting_id, count in plan.missing_counts.items():
        by_count[count].append(posting_id)
    for count, posting_ids in by_count.items():
        for chunk in _chunks(posting_ids):
            session.execute(
                update(PostingRow).where(PostingRow.id.in_(chunk)).values(missing_count=count)
            )
    for chunk in _chunks(plan.closed_ids):
        session.execute(update(PostingRow).where(PostingRow.id.in_(chunk)).values(closed_at=now))


def close_reported(
    session: Session, *, source: str, external_ids: Sequence[str], now: datetime
) -> int:
    """Close stored open postings that the source itself reports closed (Simplify's
    ``active: false``). Returns how many were open."""
    closed = 0
    for chunk in _chunks(list(dict.fromkeys(external_ids))):
        result = session.execute(
            update(PostingRow)
            .where(
                PostingRow.source == source,
                PostingRow.external_id.in_(chunk),
                PostingRow.closed_at.is_(None),
            )
            .values(closed_at=now)
        )
        closed += cast(CursorResult[Any], result).rowcount
    return closed


# --- inserting and deduplicating --------------------------------------------------------


def _dialect_insert(session: Session) -> Callable[..., Any]:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        return sqlite.insert
    if dialect == "postgresql":
        return postgresql.insert
    raise RuntimeError(f"unsupported database dialect {dialect!r}")


def insert_if_absent(
    session: Session,
    posting: Posting,
    *,
    company_id: int | None,
    fingerprint: str,
    tier: Tier,
    tier_reason: str | None,
    now: datetime,
) -> int | None:
    """Insert under (source, external_id). Returns the new id, or ``None`` if it existed:
    a posting is new exactly when this insert succeeds."""
    values = {
        "source": posting.source,
        "external_id": posting.external_id,
        "company_id": company_id,
        "company_name": posting.company_name,
        "title": posting.title,
        "locations": list(posting.locations),
        "is_remote": posting.is_remote,
        "workplace_type": posting.workplace_type,
        "department": posting.department,
        "canonical_url": posting.canonical_url,
        "fingerprint": fingerprint,
        "url": posting.url,
        "apply_url": posting.apply_url,
        "description_text": posting.description_text,
        "compensation": posting.compensation,
        "posted_at": posting.posted_at,
        "posted_at_precision": posting.posted_at_precision,
        "first_seen_at": now,
        "last_seen_at": now,
        "tier": tier,
        "tier_reason": tier_reason,
        "also_seen_on": [],
        "raw": posting.raw,
    }
    statement = (
        _dialect_insert(session)(PostingRow)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["source", "external_id"])
        .returning(PostingRow.id)
    )
    inserted: int | None = session.execute(statement).scalar_one_or_none()
    return inserted


def greenhouse_hosts(session: Session) -> dict[str, str]:
    """Custom career domains seen in Greenhouse ``absolute_url``s, mapped to board tokens."""
    rows = session.execute(
        select(Company.token, func.min(PostingRow.url))
        .join(PostingRow, PostingRow.company_id == Company.id)
        .where(Company.ats_type == "greenhouse", PostingRow.source == "greenhouse")
        .group_by(Company.token)
    )
    hosts: dict[str, str] = {}
    for token, url in rows:
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        if host and not host.endswith("greenhouse.io"):
            hosts[host] = token
    return hosts


def find_merge_target(
    session: Session,
    posting: Posting,
    *,
    fingerprint: str,
    exclude_id: int,
    since: datetime,
    greenhouse_hosts: dict[str, str],
) -> tuple[PostingRow, str] | None:
    """The posting a new job-board or feed posting duplicates, and how it matched.

    In order: the same ATS job key (rule 5a/5b); a ``gh_jid`` that names a stored Greenhouse
    job on a domain whose token isn't known yet; the fingerprint among postings seen since
    ``since`` (rule 5c). Plain career-page URLs never match on their own.
    """
    key = posting.canonical_url
    if key.startswith(("http://", "https://")):
        key = canonicalize_url(key, greenhouse_hosts=greenhouse_hosts)
    if key.startswith(_ATS_KEY_PREFIXES):
        row = session.scalars(
            select(PostingRow)
            .where(
                PostingRow.canonical_url == key,
                PostingRow.merged_into_id.is_(None),
                PostingRow.id != exclude_id,
            )
            .order_by(_ATS_ROWS_FIRST, PostingRow.first_seen_at, PostingRow.id)
            .limit(1)
        ).first()
        if row is not None:
            return row, "canonical url"
    job_id = gh_jid(posting.canonical_url) or (
        gh_jid(posting.apply_url) if posting.apply_url else None
    )
    if job_id is not None:
        row = session.scalars(
            select(PostingRow).where(
                PostingRow.source == "greenhouse",
                PostingRow.external_id == job_id,
                PostingRow.merged_into_id.is_(None),
            )
        ).first()
        if row is not None:
            return row, "gh_jid"
    row = session.scalars(
        select(PostingRow)
        .where(
            PostingRow.fingerprint == fingerprint,
            PostingRow.merged_into_id.is_(None),
            PostingRow.id != exclude_id,
            PostingRow.last_seen_at >= since,
        )
        .order_by(_ATS_ROWS_FIRST, PostingRow.first_seen_at, PostingRow.id)
        .limit(1)
    ).first()
    if row is not None:
        return row, "fingerprint"
    return None


def merge_into(
    session: Session,
    *,
    primary: PostingRow,
    duplicate_id: int,
    posting: Posting,
    how: str,
    now: datetime,
) -> None:
    """Record the duplicate on the primary (``also_seen_on``), fill the primary's gaps, and
    point the duplicate's row at it. The duplicate never alerts."""
    primary.also_seen_on = merge_also_seen(
        primary.also_seen_on,
        {
            "source": posting.source,
            "external_id": posting.external_id,
            "url": posting.url,
            "matched_by": how,
            "seen_at": now.isoformat(),
        },
    )
    if primary.description_text is None and posting.description_text:
        primary.description_text = posting.description_text
    if primary.department is None and posting.department:
        primary.department = posting.department
    if primary.compensation is None and posting.compensation:
        primary.compensation = posting.compensation
    if primary.applicant_label is None and posting.applicants is not None:
        primary.applicant_label = posting.applicants.label
        primary.applicant_min = posting.applicants.minimum
        primary.applicant_max = posting.applicants.maximum
    session.execute(
        update(PostingRow)
        .where(PostingRow.id == duplicate_id)
        .values(
            merged_into_id=primary.id,
            tier="merged",
            tier_reason=f"duplicate of posting {primary.id} ({how})",
        )
    )


def adopt_copies(
    session: Session,
    *,
    inserted: Sequence[tuple[int, Posting, str]],
    since: datetime,
    now: datetime,
    baseline: bool,
) -> list[Adoption]:
    """The reverse link: new ATS rows ``(row_id, posting, fingerprint)`` take over unmerged
    feed and job-board copies of the same job (same ATS key, ``gh_jid`` or fingerprint seen
    since ``since``), so the ATS row is the primary and the job is never pushed twice.

    The ATS row inherits the copy's notification mark, status, applicant data and any field
    it lacks; in a baseline it also takes over a match tier and score, so a
    pushed job doesn't vanish into a silent baseline. Rows merged into the copy follow it.
    """
    by_key: dict[str, int] = {}
    by_fingerprint: dict[str, int] = {}
    by_job_id: dict[str, int] = {}
    for row_id, posting, fingerprint in inserted:
        if posting.canonical_url.startswith(_ATS_KEY_PREFIXES):
            by_key.setdefault(posting.canonical_url, row_id)
        by_fingerprint.setdefault(fingerprint, row_id)
        if posting.source == "greenhouse":
            by_job_id.setdefault(posting.external_id, row_id)
    if not by_fingerprint:
        return []

    copies: dict[int, PostingRow] = {}
    unmerged_copies = select(PostingRow).where(
        PostingRow.source.in_(sorted(DEDUPE_SOURCES)), PostingRow.merged_into_id.is_(None)
    )
    for keys in _chunks(list(by_key)):
        query = unmerged_copies.where(PostingRow.canonical_url.in_(keys))
        copies.update({row.id: row for row in session.scalars(query)})
    for fingerprints in _chunks(list(by_fingerprint)):
        query = unmerged_copies.where(
            PostingRow.fingerprint.in_(fingerprints), PostingRow.last_seen_at >= since
        )
        copies.update({row.id: row for row in session.scalars(query)})
    if by_job_id:  # copies whose gh_jid link was on a domain not yet mapped to a token
        query = unmerged_copies.where(
            PostingRow.canonical_url.contains("gh_jid="), PostingRow.last_seen_at >= since
        )
        copies.update({row.id: row for row in session.scalars(query)})

    adoptions: list[Adoption] = []
    for copy in sorted(copies.values(), key=lambda row: row.id):
        job_id = gh_jid(copy.canonical_url) or (gh_jid(copy.apply_url) if copy.apply_url else None)
        if copy.canonical_url in by_key:
            primary_id, how = by_key[copy.canonical_url], "canonical url"
        elif job_id is not None and job_id in by_job_id:
            primary_id, how = by_job_id[job_id], "gh_jid"
        elif copy.fingerprint in by_fingerprint:
            primary_id, how = by_fingerprint[copy.fingerprint], "fingerprint"
        else:
            continue
        primary = session.get(PostingRow, primary_id)
        if primary is None:
            continue
        _adopt(session, primary, copy, how=how, baseline=baseline)
        adoptions.append(Adoption(primary.id, copy.id, copy.source, how))
    return adoptions


_INHERITED = (
    "notified_at",
    "applicant_label",
    "applicant_min",
    "applicant_max",
    "description_text",
    "department",
    "compensation",
)


def _adopt(
    session: Session, primary: PostingRow, copy: PostingRow, *, how: str, baseline: bool
) -> None:
    seen = merge_also_seen(
        primary.also_seen_on,
        {
            "source": copy.source,
            "external_id": copy.external_id,
            "url": copy.url,
            "matched_by": how,
            "seen_at": copy.first_seen_at.isoformat(),
        },
    )
    for entry in copy.also_seen_on or []:
        seen = merge_also_seen(seen, entry)
    primary.also_seen_on = seen
    for field in _INHERITED:
        if getattr(primary, field) is None and getattr(copy, field) is not None:
            setattr(primary, field, getattr(copy, field))
    if primary.status == "New" and copy.status != "New":
        primary.status = copy.status
    if baseline and copy.tier == "instant":
        primary.tier = copy.tier
        primary.tier_reason = f"{copy.tier_reason} (first seen on {copy.source})"
        primary.score = copy.score
        primary.score_breakdown = copy.score_breakdown
    copy.merged_into_id = primary.id
    copy.tier = "merged"
    copy.tier_reason = f"duplicate of posting {primary.id} ({how}), seen before it"
    session.execute(
        update(PostingRow)
        .where(PostingRow.merged_into_id == copy.id)
        .values(merged_into_id=primary.id)
    )


# --- decisions and notifications --------------------------------------------------------


def set_decision(session: Session, row_id: int, decision: Decision) -> None:
    """A decision on a stored posting. The years asked stay unless this decision read them."""
    values: dict[str, Any] = {
        "tier": decision.tier,
        "tier_reason": decision.reason,
        "score": decision.score,
        "score_breakdown": decision.breakdown,
    }
    if decision.years_required is not None:
        values["years_required"] = decision.years_required
    session.execute(update(PostingRow).where(PostingRow.id == row_id).values(**values))


def save_evaluation(session: Session, row_id: int, posting: Posting, decision: Decision) -> None:
    """Store what enrichment added and the final decision. A detail call can name places the
    list didn't (Workday's "2 Locations"), so the fingerprint is taken again."""
    applicants = posting.applicants
    session.execute(
        update(PostingRow)
        .where(PostingRow.id == row_id)
        .values(
            description_text=posting.description_text,
            department=posting.department,
            compensation=posting.compensation,
            posted_at=posting.posted_at,
            posted_at_precision=posting.posted_at_precision,
            locations=list(posting.locations),
            is_remote=posting.is_remote,
            workplace_type=posting.workplace_type,
            fingerprint=posting_fingerprint(posting),
            applicant_label=applicants.label if applicants else None,
            applicant_min=applicants.minimum if applicants else None,
            applicant_max=applicants.maximum if applicants else None,
            tier=decision.tier,
            tier_reason=decision.reason,
            score=decision.score,
            score_breakdown=decision.breakdown,
            years_required=decision.years_required,
        )
    )


def pending_rows(
    session: Session,
    *,
    source: str,
    company_id: int | None,
    since: datetime,
    exclude_ids: Collection[int] = (),
) -> list[PostingRow]:
    """Postings an interrupted cycle inserted but never decided."""
    query = select(PostingRow).where(
        PostingRow.tier == "pending",
        PostingRow.source == source,
        PostingRow.merged_into_id.is_(None),
        PostingRow.first_seen_at >= since,
    )
    if company_id is None:
        query = query.where(PostingRow.company_id.is_(None))
    else:
        query = query.where(PostingRow.company_id == company_id)
    if exclude_ids:
        query = query.where(PostingRow.id.not_in(list(exclude_ids)))
    return list(session.scalars(query.order_by(PostingRow.id)))


def instant_unnotified(
    session: Session, *, since: datetime, sources: Collection[str] | None = None
) -> list[PostingRow]:
    """Open, untriaged matches whose push hasn't gone out, best first; only from ``sources``
    when given."""
    query = select(PostingRow).where(
        PostingRow.tier == "instant",
        PostingRow.notified_at.is_(None),
        PostingRow.merged_into_id.is_(None),
        PostingRow.closed_at.is_(None),
        PostingRow.status == "New",
        PostingRow.first_seen_at >= since,
    )
    if sources is not None:
        query = query.where(PostingRow.source.in_(list(sources)))
    return list(
        session.scalars(
            query.order_by(PostingRow.score.desc(), PostingRow.first_seen_at, PostingRow.id)
        )
    )


def mark_notified(session: Session, row_ids: Collection[int], *, now: datetime) -> None:
    if row_ids:
        session.execute(
            update(PostingRow).where(PostingRow.id.in_(list(row_ids))).values(notified_at=now)
        )


# --- dashboard -----------------------------------------------------------------------------


def set_status(session: Session, row_id: int, status: str, *, now: datetime) -> str | None:
    """Change a posting's status (New, Saved, Applied, Dismissed), noting when, and return the
    one it had; ``None`` if there's no such posting."""
    row = session.get(PostingRow, row_id)
    if row is None:
        return None
    previous = row.status
    if previous != status:
        row.status = status
        row.status_changed_at = now
        session.flush()
    return previous


def set_statuses(session: Session, row_ids: Collection[int], status: str, *, now: datetime) -> int:
    """Move many postings at once (bulk dismiss and its undo); how many changed."""
    changed = 0
    for chunk in _chunks(sorted(set(row_ids))):
        result = cast(
            CursorResult[Any],
            session.execute(
                update(PostingRow)
                .where(PostingRow.id.in_(chunk), PostingRow.status != status)
                .values(status=status, status_changed_at=now)
            ),
        )
        changed += result.rowcount
    session.flush()
    return changed


# --- the dashboard's blocklist ----------------------------------------------------------------


def blocked_companies(session: Session) -> list[BlockedCompany]:
    return list(session.scalars(select(BlockedCompany).order_by(BlockedCompany.name)))


def block_company(session: Session, name: str, *, now: datetime) -> bool:
    """Block a company by name, compared like the config blocklist ("Acme, Inc." is "Acme");
    ``False`` if it was already blocked here."""
    key = normalize_company(name)
    if not key:
        raise ValueError("a company needs a name to be blocked")
    if session.scalar(select(BlockedCompany.id).where(BlockedCompany.name_key == key)):
        return False
    session.add(BlockedCompany(name=" ".join(name.split()), name_key=key, created_at=now))
    session.flush()
    return True


def unblock_company(session: Session, name: str) -> bool:
    result = cast(
        CursorResult[Any],
        session.execute(
            delete(BlockedCompany).where(BlockedCompany.name_key == normalize_company(name))
        ),
    )
    return result.rowcount > 0


def blocklist(session: Session, base: Blocklist) -> Blocklist:
    """config.yaml's blocklist plus the companies blocked from the dashboard."""
    keys = set(session.scalars(select(BlockedCompany.name_key)))
    if not keys:
        return base
    return Blocklist(companies=base.companies | keys, boards=base.boards)


def update_company(
    session: Session, company_id: int, *, priority: int | None = None, active: bool | None = None
) -> Company | None:
    """Change a company's priority or pause it; ``None`` if there's no such company."""
    company = session.get(Company, company_id)
    if company is None:
        return None
    if priority is not None:
        company.priority = priority
    if active is not None:
        company.active = active
    session.flush()
    return company


# --- the LLM layer --------------------------------------------------------------------------


def cached_assessments(session: Session, questions: Mapping[int, str]) -> dict[int, Assessment]:
    """The stored answers to ``questions`` ({posting id: input hash}), by posting id. Only an
    answer to that exact question counts; failed calls are never answers."""
    found: dict[int, Assessment] = {}
    for chunk in _chunks(list(questions)):
        rows = session.scalars(
            select(LlmAssessment)
            .where(LlmAssessment.posting_id.in_(chunk), LlmAssessment.ok.is_(True))
            .order_by(LlmAssessment.id)
        )
        for row in rows:
            if row.input_hash == questions[row.posting_id] and row.level in LEVELS:
                found[row.posting_id] = Assessment(
                    level=row.level,
                    backend_focus=row.backend_focus or 0.0,
                    fit=row.fit or 0.0,
                    reason=row.reason or "",
                )
    return found


def count_llm_calls(session: Session, *, since: datetime) -> int:
    """LLM calls made since ``since``, failed ones included: what the daily cap counts."""
    count = session.scalar(
        select(func.count(LlmAssessment.id)).where(LlmAssessment.created_at >= since)
    )
    return int(count or 0)


def record_assessment(
    session: Session, *, posting_id: int, input_hash: str, reply: LlmReply, now: datetime
) -> None:
    answer = reply.assessment
    session.add(
        LlmAssessment(
            posting_id=posting_id,
            input_hash=input_hash,
            model=reply.model,
            created_at=now,
            ok=answer is not None,
            level=answer.level if answer else None,
            backend_focus=answer.backend_focus if answer else None,
            fit=answer.fit if answer else None,
            reason=answer.reason if answer else None,
            error=reply.error[:MAX_ERROR_CHARS] if reply.error else None,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
        )
    )
    session.flush()


# --- runs ---------------------------------------------------------------------------------


def start_run(
    session: Session, *, source: str, target: str, started_at: datetime, result: FetchResult
) -> int:
    run = Run(
        source=source,
        target=target,
        started_at=started_at,
        ok=result.ok,
        fetched_count=len(result.postings),
        error=result.error[:MAX_ERROR_CHARS] if result.error else None,
        error_kind=result.error_kind,
        not_modified=result.not_modified,
        warnings=list(result.warnings) or None,
    )
    session.add(run)
    session.flush()
    return run.id


def finish_run(
    session: Session, run_id: int, *, finished_at: datetime, new_count: int, stats: dict[str, int]
) -> None:
    session.execute(
        update(Run)
        .where(Run.id == run_id)
        .values(finished_at=finished_at, new_count=new_count, stats=stats)
    )


def delete_runs_before(session: Session, cutoff: datetime) -> int:
    """Delete runs that started before ``cutoff`` (``retention.runs_days``); returns how many."""
    result = session.execute(delete(Run).where(Run.started_at < cutoff))
    return cast(CursorResult[Any], result).rowcount


# --- health -------------------------------------------------------------------------------


def recent_runs(
    session: Session,
    *,
    source: str,
    target: str,
    limit: int | None = None,
    since: datetime | None = None,
) -> list[Run]:
    """One target's runs, newest first."""
    query = (
        select(Run)
        .where(Run.source == source, Run.target == target)
        .order_by(Run.started_at.desc(), Run.id.desc())
    )
    if since is not None:
        query = query.where(Run.started_at >= since)
    if limit is not None:
        query = query.limit(limit)
    return list(session.scalars(query))


def latest_counted_run(session: Session, *, source: str, target: str) -> Run | None:
    """The newest successful run that brought a body (a 304 says nothing about counts)."""
    return session.scalars(
        select(Run)
        .where(
            Run.source == source,
            Run.target == target,
            Run.ok.is_(True),
            Run.not_modified.is_(False),
        )
        .order_by(Run.started_at.desc(), Run.id.desc())
        .limit(1)
    ).first()


def open_incidents(session: Session, *, source: str, target: str) -> dict[str, HealthIncident]:
    rows = session.scalars(
        select(HealthIncident).where(
            HealthIncident.source == source,
            HealthIncident.target == target,
            HealthIncident.resolved_at.is_(None),
        )
    )
    return {row.kind: row for row in rows}


def open_incident(
    session: Session,
    *,
    source: str,
    target: str,
    kind: str,
    opened_at: datetime,
    title: str,
    detail: str,
) -> HealthIncident:
    incident = HealthIncident(
        source=source, target=target, kind=kind, opened_at=opened_at, title=title, detail=detail
    )
    session.add(incident)
    session.flush()
    return incident


def incidents_due(session: Session) -> list[HealthIncident]:
    """Open incidents whose alert hasn't gone out, and resolved ones whose alert did but
    whose recovery message hasn't."""
    return list(
        session.scalars(
            select(HealthIncident)
            .where(
                or_(
                    HealthIncident.resolved_at.is_(None) & HealthIncident.alerted_at.is_(None),
                    HealthIncident.resolved_at.is_not(None)
                    & HealthIncident.alerted_at.is_not(None)
                    & HealthIncident.recovery_sent_at.is_(None),
                )
            )
            .order_by(HealthIncident.id)
        )
    )


def mark_incident_sent(
    session: Session, incident_id: int, *, recovery: bool, now: datetime
) -> None:
    values = {"recovery_sent_at": now} if recovery else {"alerted_at": now}
    session.execute(update(HealthIncident).where(HealthIncident.id == incident_id).values(**values))


def row_to_posting(row: PostingRow, *, company_token: str | None = None) -> Posting:
    return Posting(
        source=cast(SourceName, row.source),
        external_id=row.external_id,
        company_name=row.company_name,
        company_token=company_token,
        title=row.title,
        locations=tuple(row.locations or ()),
        is_remote=row.is_remote,
        workplace_type=row.workplace_type,
        url=row.url,
        apply_url=row.apply_url,
        canonical_url=row.canonical_url,
        posted_at=row.posted_at,
        posted_at_precision=cast(Precision, row.posted_at_precision),
        description_text=row.description_text,
        department=row.department,
        compensation=row.compensation,
        raw=dict(row.raw or {}),
        applicants=(
            ApplicantCount(row.applicant_label, row.applicant_min, row.applicant_max)
            if row.applicant_label
            else None
        ),
    )
