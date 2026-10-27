"""Read models for the dashboard: posting lists and counts per status view, and source health.

The New view holds matches (the instant tier) nobody has triaged yet. Saved, Applied and
Dismissed hold every posting with that status, whatever its tier. Merged copies never show;
their primary does, with ``also_seen_on``. Companies on the blocklist are hidden everywhere
except Applied, which doubles as the record of your applications.
"""

from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import ColumnElement, case, func, or_, select
from sqlalchemy.orm import Session, defer

from jobpulse.core.filters import Blocklist
from jobpulse.core.pay import yearly_usd_range
from jobpulse.core.places import PlaceGroup, place_options, posting_in_place
from jobpulse.db.tables import Company, HealthIncident, PostingRow, Run, Search
from jobpulse.db.types import UTCDateTime

STATUSES: tuple[str, ...] = ("New", "Saved", "Applied", "Dismissed")
BLOCKLIST_EXEMPT_STATUSES = frozenset({"Applied"})
PAGE_SIZE = 50

SortOrder = Literal["score", "fresh", "pushed", "moved"]
MOVED_SORT_LABELS = {
    "Saved": "Recently saved",
    "Applied": "Recently applied",
    "Dismissed": "Recently dismissed",
}

# Years of experience asked: key → (at least, at most); "unstated" is no number at all.
EXPERIENCE: dict[str, tuple[int | None, int | None]] = {
    "0-2": (None, 2),
    "3-5": (3, 5),
    "6+": (6, None),
    "unstated": (None, None),
}
EXPERIENCE_CHOICES: tuple[tuple[str, str], ...] = (
    ("0-2", "Asks 0–2 yrs"),
    ("3-5", "Asks 3–5 yrs"),
    ("6+", "Asks 6+ yrs"),
    ("unstated", "Years not stated"),
)

# How long ago a posting was pushed: key → (age, whether it means "longer ago than that").
PUSHED_WINDOWS: dict[str, tuple[timedelta, bool]] = {
    "1h": (timedelta(hours=1), False),
    "3h": (timedelta(hours=3), False),
    "6h": (timedelta(hours=6), False),
    "12h": (timedelta(hours=12), False),
    "24h": (timedelta(hours=24), False),
    "3d": (timedelta(days=3), False),
    "7d": (timedelta(days=7), False),
    "30d": (timedelta(days=30), False),
    "over-1d": (timedelta(days=1), True),
    "over-3d": (timedelta(days=3), True),
    "over-7d": (timedelta(days=7), True),
    "over-30d": (timedelta(days=30), True),
}
PUSHED_CHOICES: tuple[tuple[str, str], ...] = (
    ("1h", "Pushed in the last hour"),
    ("3h", "Last 3 hours"),
    ("6h", "Last 6 hours"),
    ("12h", "Last 12 hours"),
    ("24h", "Last 24 hours"),
    ("3d", "Last 3 days"),
    ("7d", "Last 7 days"),
    ("30d", "Last 30 days"),
    ("over-1d", "Pushed over a day ago"),
    ("over-3d", "Over 3 days ago"),
    ("over-7d", "Over a week ago"),
    ("over-30d", "Over 30 days ago"),
)


@dataclass(frozen=True, slots=True)
class PostingQuery:
    status: str = "New"
    source: str | None = None
    company: str | None = None  # part of the company name, any case
    remote: bool | None = None  # True: remote only; False: explicitly not remote
    min_score: float | None = None
    text: str | None = None  # part of the title, company or description, any case
    open_only: bool = False
    place: str | None = None  # a key from core.places: "US", "US-TX", "metro:dc", "US-TX/Austin"
    pushed: str | None = None  # a PUSHED_WINDOWS key
    min_pay: int | None = None  # thousands of US dollars a year, the top of the range
    pay_listed: bool = False  # only postings with pay in dollars
    experience: str | None = None  # an EXPERIENCE key
    sort: SortOrder = "score"
    page: int = 1
    page_size: int = PAGE_SIZE

    @property
    def more_filters(self) -> bool:
        """A filter under "More filters" is set, so the form shows them open."""
        return bool(
            self.company
            or self.source
            or self.remote is not None
            or self.min_score is not None
            or self.open_only
            or self.min_pay
            or self.pay_listed
            or self.experience
        )

    @property
    def filtered(self) -> bool:
        return self.more_filters or bool(self.text or self.place or self.pushed)


@dataclass(frozen=True, slots=True)
class PostingPage:
    rows: list[PostingRow]
    total: int
    page: int
    page_size: int
    places: tuple[PlaceGroup, ...] = ()  # the location options, with counts

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


def pushed_at() -> ColumnElement[datetime]:
    """When jobpulse pushed a posting, or first saw it if it never pushed it. The type makes
    SQLite compare against bound times in the stored format."""
    return func.coalesce(PostingRow.notified_at, PostingRow.first_seen_at, type_=UTCDateTime())


def _pays(compensation: Mapping[str, Any] | None, query: PostingQuery) -> bool:
    found = yearly_usd_range(compensation)
    if found is None:
        return not query.pay_listed
    return not query.min_pay or found[1] >= query.min_pay * 1000


def _conditions(
    query: PostingQuery, hidden: Collection[str], now: datetime
) -> list[ColumnElement[bool]]:
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
    if query.pushed in PUSHED_WINDOWS:
        age, older = PUSHED_WINDOWS[query.pushed]
        conditions.append(pushed_at() < now - age if older else pushed_at() >= now - age)
    if query.experience in EXPERIENCE:
        least, most = EXPERIENCE[query.experience]
        if least is None and most is None:
            conditions.append(PostingRow.years_required.is_(None))
        if least is not None:
            conditions.append(PostingRow.years_required >= least)
        if most is not None:
            conditions.append(PostingRow.years_required <= most)
    return conditions


def _order(sort: SortOrder) -> tuple[Any, ...]:
    # Freshness: the source's own timestamp when it has one, else when jobpulse first saw it.
    freshness = func.coalesce(PostingRow.posted_at, PostingRow.first_seen_at).desc()
    by_score = PostingRow.score.desc().nulls_last()
    if sort == "pushed":
        return pushed_at().desc(), by_score
    if sort == "moved":
        return PostingRow.status_changed_at.desc().nulls_last(), pushed_at().desc()
    return (by_score, freshness) if sort == "score" else (freshness, by_score)


def _matches(
    session: Session,
    query: PostingQuery,
    *,
    now: datetime,
    hidden: Collection[str],
    as_of: datetime | None = None,
) -> tuple[list[int], tuple[PlaceGroup, ...]]:
    """The ids of every posting the query shows, in order, and the location options. The SQL
    filters run in the database; pay and location are read in Python (pay comes in several
    shapes, places from the label tables) over the ids and labels the SQL filters leave.

    With ``as_of``, only postings that were in the view by then: pushed (or first seen) and
    last moved no later."""
    conditions = _conditions(query, hidden, now)
    if as_of is not None:
        conditions += [
            pushed_at() <= as_of,
            or_(PostingRow.status_changed_at.is_(None), PostingRow.status_changed_at <= as_of),
        ]
    columns: list[Any] = [PostingRow.id, PostingRow.locations]
    pay_filter = bool(query.min_pay or query.pay_listed)
    if pay_filter:
        columns.append(PostingRow.compensation)
    found = session.execute(
        select(*columns).where(*conditions).order_by(*_order(query.sort), PostingRow.id.desc())
    ).all()
    if pay_filter:
        found = [row for row in found if _pays(row.compensation, query)]
    places = place_options((row.locations or () for row in found), selected=query.place)
    if query.place:
        found = [row for row in found if posting_in_place(row.locations or (), query.place)]
    return [row.id for row in found], places


def matching_ids(
    session: Session,
    query: PostingQuery,
    *,
    now: datetime,
    hidden: Collection[str] = (),
    as_of: datetime | None = None,
) -> list[int]:
    """Every posting the query shows, on any page (bulk moves, CSV export)."""
    return _matches(session, query, now=now, hidden=hidden, as_of=as_of)[0]


def load_postings(session: Session, ids: Sequence[int]) -> list[PostingRow]:
    """These postings, in this order, without their raw data or description."""
    loaded: dict[int, PostingRow] = {}
    for start in range(0, len(ids), PAGE_SIZE * 10):
        loaded.update(
            (row.id, row)
            for row in session.scalars(
                select(PostingRow)
                .where(PostingRow.id.in_(ids[start : start + PAGE_SIZE * 10]))
                .options(
                    defer(PostingRow.raw, raiseload=True),
                    defer(PostingRow.description_text, raiseload=True),
                )
            )
        )
    return [loaded[posting_id] for posting_id in ids if posting_id in loaded]


def list_postings(
    session: Session, query: PostingQuery, *, now: datetime, hidden: Collection[str] = ()
) -> PostingPage:
    """One page of a view, with the location options for the whole view."""
    ids, places = _matches(session, query, now=now, hidden=hidden)
    start = (query.page - 1) * query.page_size
    rows = load_postings(session, ids[start : start + query.page_size])
    return PostingPage(rows, len(ids), query.page, query.page_size, places)


def company_postings(session: Session, name: str) -> int:
    """How many postings the views that hide blocked companies show for this company (names
    compared like the blocklist: "Acme, Inc." is "ACME")."""
    statuses = [status for status in STATUSES if status not in BLOCKLIST_EXEMPT_STATUSES]
    names = [
        found
        for found in session.scalars(
            select(PostingRow.company_name)
            .where(PostingRow.merged_into_id.is_(None), PostingRow.status.in_(statuses))
            .distinct()
        )
        if Blocklist.build(companies=[name]).blocks_company(found)
    ]
    if not names:
        return 0
    return sum(
        session.scalar(
            select(func.count())
            .select_from(PostingRow)
            .where(*_in_view(view, ()), PostingRow.company_name.in_(names))
        )
        or 0
        for view in statuses
    )


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
