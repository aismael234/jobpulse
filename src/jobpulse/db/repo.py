"""Repository functions over the ORM models.

Everything here is synchronous and leaves the transaction to the caller: the poller commits
between phases (or only flushes, in a dry run), and never awaits while a transaction is open.
"""

from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from jobpulse.config import WatchlistEntry
from jobpulse.core.atsurls import gh_jid
from jobpulse.core.dedupe import canonicalize_url, merge_also_seen
from jobpulse.core.detection import PresencePlan, PresenceRow
from jobpulse.core.models import (
    ATS_SOURCES,
    AtsName,
    CompanyTarget,
    Decision,
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
from jobpulse.db.tables import Company, PostingRow, Run, Search

CHUNK = 500
MAX_ERROR_CHARS = 2000
_ATS_KEY_PREFIXES = tuple(f"{ats}:" for ats in sorted(ATS_SOURCES))


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


@dataclass(frozen=True, slots=True)
class SyncReport:
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    deactivated: tuple[str, ...] = ()


def _chunks(ids: Sequence[int]) -> Iterator[Sequence[int]]:
    for start in range(0, len(ids), CHUNK):
        yield ids[start : start + CHUNK]


# --- watchlist ---------------------------------------------------------------------------


def sync_watchlist(
    session: Session, entries: Sequence[WatchlistEntry], *, now: datetime
) -> SyncReport:
    """Idempotent upsert of the config watchlist on (ats_type, token).

    Config entries own their rows: a company that came from the dashboard or discovery and
    is then added to config becomes config-owned. Config-owned companies that left config are
    deactivated, never deleted, so their history stays.
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
        }
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
    else:
        session.execute(
            update(Search).where(Search.id == target.search_id).values(baseline_done_at=now)
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
            .order_by(PostingRow.first_seen_at, PostingRow.id)
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
        .order_by(PostingRow.first_seen_at, PostingRow.id)
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
    session.execute(
        update(PostingRow)
        .where(PostingRow.id == duplicate_id)
        .values(
            merged_into_id=primary.id,
            tier="merged",
            tier_reason=f"duplicate of posting {primary.id} ({how})",
        )
    )


# --- decisions and notifications --------------------------------------------------------


def set_decision(session: Session, row_id: int, decision: Decision) -> None:
    session.execute(
        update(PostingRow)
        .where(PostingRow.id == row_id)
        .values(
            tier=decision.tier,
            tier_reason=decision.reason,
            score=decision.score,
            score_breakdown=decision.breakdown,
        )
    )


def save_evaluation(session: Session, row_id: int, posting: Posting, decision: Decision) -> None:
    """Store what enrichment added and the final decision."""
    session.execute(
        update(PostingRow)
        .where(PostingRow.id == row_id)
        .values(
            description_text=posting.description_text,
            department=posting.department,
            compensation=posting.compensation,
            posted_at=posting.posted_at,
            posted_at_precision=posting.posted_at_precision,
            tier=decision.tier,
            tier_reason=decision.reason,
            score=decision.score,
            score_breakdown=decision.breakdown,
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


def instant_unnotified(session: Session, *, since: datetime) -> list[PostingRow]:
    """Instant-tier postings whose push hasn't gone out, best first."""
    return list(
        session.scalars(
            select(PostingRow)
            .where(
                PostingRow.tier == "instant",
                PostingRow.notified_at.is_(None),
                PostingRow.merged_into_id.is_(None),
                PostingRow.first_seen_at >= since,
            )
            .order_by(PostingRow.score.desc(), PostingRow.first_seen_at, PostingRow.id)
        )
    )


def hold_back(session: Session, row_id: int, *, reason: str) -> None:
    session.execute(
        update(PostingRow).where(PostingRow.id == row_id).values(tier="digest", tier_reason=reason)
    )


def mark_notified(session: Session, row_id: int, *, now: datetime) -> None:
    session.execute(update(PostingRow).where(PostingRow.id == row_id).values(notified_at=now))


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
    )
