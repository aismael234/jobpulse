"""Read models for the dashboard: posting lists and counts per status view, and source health.

The New view holds matches (the instant tier) nobody has triaged yet. Saved, Applied and
Dismissed hold every posting with that status, whatever its tier. Merged copies never show;
their primary does, with ``also_seen_on``. Companies on the blocklist are hidden everywhere
except Applied, which doubles as the record of your applications.
"""

from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import ColumnElement, case, func, or_, select
from sqlalchemy.orm import Session, defer

from jobpulse.core.filters import Blocklist
from jobpulse.db.tables import Company, HealthIncident, PostingRow, Run, Search

STATUSES: tuple[str, ...] = ("New", "Saved", "Applied", "Dismissed")
BLOCKLIST_EXEMPT_STATUSES = frozenset({"Applied"})
PAGE_SIZE = 50

SortOrder = Literal["score", "fresh"]


@dataclass(frozen=True, slots=True)
class PostingQuery:
    status: str = "New"
    source: str | None = None
    company: str | None = None  # part of the company name, any case
    remote: bool | None = None  # True: remote only; False: explicitly not remote
    min_score: float | None = None
    text: str | None = None  # part of the title, company or description, any case
    open_only: bool = False
    sort: SortOrder = "score"
    page: int = 1
    page_size: int = PAGE_SIZE


@dataclass(frozen=True, slots=True)
class PostingPage:
    rows: list[PostingRow]
    total: int
    page: int
    page_size: int

    @property
    def has_next(self) -> bool:
        return self.page * self.page_size < self.total

    @property
    def has_previous(self) -> bool:
        return self.page > 1


def _in_view(status: str, hidden: Collection[str]) -> list[ColumnElement[bool]]:
    conditions = [PostingRow.merged_into_id.is_(None), PostingRow.status == status]
    if status == "New":
        conditions.append(PostingRow.tier == "instant")
    if hidden and status not in BLOCKLIST_EXEMPT_STATUSES:
        conditions.append(PostingRow.company_name.not_in(sorted(hidden)))
    return conditions


def _contains(column: Any, text: str) -> ColumnElement[bool]:
    """Case-insensitive substring match; ``%`` and ``_`` in ``text`` are literal."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    condition: ColumnElement[bool] = column.ilike(f"%{escaped}%", escape="\\")
    return condition


def hidden_companies(session: Session, blocklist: Blocklist) -> frozenset[str]:
    """Company names, as stored, that the blocklist hides from the dashboard."""
    if not blocklist.companies:
        return frozenset()
    names = session.scalars(
        select(PostingRow.company_name)
        .where(
            PostingRow.merged_into_id.is_(None),
            or_(PostingRow.tier == "instant", PostingRow.status != "New"),
        )
        .distinct()
    )
    return frozenset(name for name in names if blocklist.blocks_company(name))


def list_postings(
    session: Session, query: PostingQuery, *, hidden: Collection[str] = ()
) -> PostingPage:
    conditions = _in_view(query.status, hidden)
    if query.source:
        conditions.append(PostingRow.source == query.source)
    if query.company:
        conditions.append(_contains(PostingRow.company_name, query.company))
    if query.remote is not None:
        conditions.append(PostingRow.is_remote.is_(query.remote))
    if query.min_score is not None:
        conditions.append(PostingRow.score >= query.min_score)
    if query.text:
        conditions.append(
            or_(
                _contains(PostingRow.title, query.text),
                _contains(PostingRow.company_name, query.text),
                _contains(PostingRow.description_text, query.text),
            )
        )
    if query.open_only:
        conditions.append(PostingRow.closed_at.is_(None))
    total = session.scalar(select(func.count()).select_from(PostingRow).where(*conditions)) or 0
    # Freshness: the source's own timestamp when it has one, else when jobpulse first saw it.
    freshness = func.coalesce(PostingRow.posted_at, PostingRow.first_seen_at).desc()
    by_score = PostingRow.score.desc().nulls_last()
    order = (by_score, freshness) if query.sort == "score" else (freshness, by_score)
    rows = session.scalars(
        select(PostingRow)
        .where(*conditions)
        .options(
            defer(PostingRow.raw, raiseload=True),
            defer(PostingRow.description_text, raiseload=True),
        )
        .order_by(*order, PostingRow.id.desc())
        .limit(query.page_size)
        .offset((query.page - 1) * query.page_size)
    )
    return PostingPage(list(rows), total, query.page, query.page_size)


def view_counts(session: Session, *, hidden: Collection[str] = ()) -> dict[str, int]:
    """How many postings each status view shows (with no other filter)."""
    return {
        status: session.scalar(
            select(func.count()).select_from(PostingRow).where(*_in_view(status, hidden))
        )
        or 0
        for status in STATUSES
    }


@dataclass(frozen=True, slots=True)
class TargetRef:
    source: str
    target: str
    label: str
    polled: bool  # still polled: active, and neither it nor its company is blocked


@dataclass(frozen=True, slots=True)
class TargetHealth:
    ref: TargetRef
    last_run_at: datetime | None = None
    last_run_ok: bool | None = None
    last_success_at: datetime | None = None
    failures_in_a_row: int = 0
    runs_24h: int = 0
    fetched_24h: int = 0
    new_24h: int = 0
    last_error: str | None = None
    last_error_kind: str | None = None
    last_error_at: datetime | None = None
    incidents: tuple[str, ...] = ()


def known_targets(
    session: Session, *, blocklist: Blocklist, feeds: Collection[tuple[str, str]] = ()
) -> list[TargetRef]:
    """Every stored company and search, plus the configured ``feeds`` (source, name)."""
    refs = [
        TargetRef(
            company.ats_type,
            company.token,
            company.name,
            company.active
            and not blocklist.blocks_board(company.ats_type, company.token)
            and not blocklist.blocks_company(company.name),
        )
        for company in session.scalars(select(Company))
    ]
    refs += [
        TargetRef(search.site, search.name, search.name, search.active)
        for search in session.scalars(select(Search))
    ]
    refs += [TargetRef(source, name, name, True) for source, name in feeds]
    return refs


def health_overview(
    session: Session, *, now: datetime, targets: Sequence[TargetRef]
) -> list[TargetHealth]:
    """Per target, from ``runs`` and the open ``health_incidents``: every target that ever
    ran, and every polled target that hasn't yet. Problems first."""
    in_window = Run.started_at >= now - timedelta(hours=24)
    stats = session.execute(
        select(
            Run.source,
            Run.target,
            func.max(Run.started_at).label("last_run_at"),
            func.max(case((Run.ok.is_(True), Run.started_at))).label("last_success_at"),
            func.count(case((in_window, 1))).label("runs_24h"),
            func.coalesce(func.sum(case((in_window, Run.fetched_count), else_=0)), 0).label(
                "fetched_24h"
            ),
            func.coalesce(func.sum(case((in_window, Run.new_count), else_=0)), 0).label("new_24h"),
            func.max(case((Run.ok.is_(False), Run.id))).label("last_failure_id"),
        ).group_by(Run.source, Run.target)
    ).all()
    failure_ids = [row.last_failure_id for row in stats if row.last_failure_id is not None]
    failures = (
        {run.id: run for run in session.scalars(select(Run).where(Run.id.in_(failure_ids)))}
        if failure_ids
        else {}
    )
    incidents: dict[tuple[str, str], list[str]] = defaultdict(list)
    for incident in open_incidents(session):
        incidents[(incident.source, incident.target)].append(incident.title)

    refs = {(ref.source, ref.target): ref for ref in targets}
    overview: list[TargetHealth] = []
    for row in stats:
        key = (row.source, row.target)
        last_ok = row.last_success_at == row.last_run_at
        streak = 0
        if not last_ok:
            count = (
                select(func.count())
                .select_from(Run)
                .where(Run.source == row.source, Run.target == row.target, Run.ok.is_(False))
            )
            if row.last_success_at is not None:
                count = count.where(Run.started_at > row.last_success_at)
            streak = session.scalar(count) or 0
        failure = failures.get(row.last_failure_id)
        # A target that ran but is no longer configured or stored isn't polled any more.
        ref = refs.pop(key) if key in refs else TargetRef(row.source, row.target, row.target, False)
        overview.append(
            TargetHealth(
                ref=ref,
                last_run_at=row.last_run_at,
                last_run_ok=last_ok,
                last_success_at=row.last_success_at,
                failures_in_a_row=streak,
                runs_24h=row.runs_24h,
                fetched_24h=row.fetched_24h,
                new_24h=row.new_24h,
                last_error=failure.error if failure else None,
                last_error_kind=failure.error_kind if failure else None,
                last_error_at=failure.started_at if failure else None,
                incidents=tuple(incidents.get(key, ())),
            )
        )
    overview += [
        TargetHealth(ref=ref, incidents=tuple(incidents.get(key, ())))
        for key, ref in refs.items()
        if ref.polled
    ]

    def problems_first(health: TargetHealth) -> tuple[int, str, str]:
        if health.incidents or health.failures_in_a_row:
            group = 0
        elif health.ref.polled:
            group = 1
        else:
            group = 2
        return group, health.ref.source, health.ref.target.lower()

    return sorted(overview, key=problems_first)


def open_incidents(session: Session) -> list[HealthIncident]:
    return list(
        session.scalars(
            select(HealthIncident)
            .where(HealthIncident.resolved_at.is_(None))
            .order_by(HealthIncident.opened_at, HealthIncident.id)
        )
    )
