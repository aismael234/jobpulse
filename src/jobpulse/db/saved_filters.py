"""Saved filters (M5.1): CRUD, the color-blind preference, and matching through the
dashboard's own query code (``dashboard.matching_ids``), so a push, a card's bullets and a
click on the filter's button agree.

A saved filter is *present* while its row exists, whether it is on or off: with none present
every match goes out in one batch per cycle; with any present only matches that fit an active,
valid one are pushed, one message each. A filter whose stored parameters no longer read back
the same (a renamed filter, say) is flagged and skipped, never widened to everything.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpulse.core.palette import SLOTS
from jobpulse.db.dashboard import PostingQuery, matching_ids
from jobpulse.db.query_params import saved_query
from jobpulse.db.tables import FilterPush, Preference, SavedFilter

MAX_FILTERS = SLOTS  # one per color
MAX_LABEL = 60
COLOR_BLIND = "color_blind"


class SavedFilterError(ValueError):
    """A save the user has to fix: no label, a taken color, more than MAX_FILTERS."""


@dataclass(frozen=True, slots=True)
class Saved:
    """A saved filter with its query read back, or why it can't be."""

    row: SavedFilter
    query: PostingQuery | None
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.row.active and self.query is not None


def list_filters(session: Session) -> list[Saved]:
    """Every saved filter, in order, each with its query read back (or why it can't be)."""
    rows = session.scalars(select(SavedFilter).order_by(SavedFilter.position, SavedFilter.slot))
    found: list[Saved] = []
    for row in rows:
        query, error = saved_query(row.params)
        found.append(Saved(row, query, error))
    return found


def _row(session: Session, filter_id: int) -> SavedFilter:
    row = session.get(SavedFilter, filter_id)
    if row is None:
        raise LookupError(f"no saved filter {filter_id}")
    return row


def _checked(
    session: Session, *, label: str, slot: int, params: Mapping[str, str], editing: int | None
) -> tuple[str, dict[str, str]]:
    label = " ".join(label.split())
    if not label:
        raise SavedFilterError("give the filter a label")
    if len(label) > MAX_LABEL:
        raise SavedFilterError(f"keep the label under {MAX_LABEL} characters")
    if not 0 <= slot < SLOTS:
        raise SavedFilterError("pick one of the colors")
    taken = session.scalar(
        select(SavedFilter.id).where(SavedFilter.slot == slot, SavedFilter.id != (editing or 0))
    )
    if taken is not None:
        raise SavedFilterError("that color is taken by another saved filter")
    query, error = saved_query(params)
    if query is None:
        raise SavedFilterError(f"these filters can't be saved: {error}")
    return label, dict(params)


def create(
    session: Session, *, label: str, slot: int, params: Mapping[str, str], now: datetime
) -> SavedFilter:
    """A new, active saved filter that fires for postings decided from ``now`` on."""
    count = session.scalar(select(func.count()).select_from(SavedFilter)) or 0
    if count >= MAX_FILTERS:
        raise SavedFilterError(f"you have {MAX_FILTERS} saved filters, the most there can be")
    label, params = _checked(session, label=label, slot=slot, params=params, editing=None)
    position = (session.scalar(select(func.max(SavedFilter.position))) or 0) + 1
    row = SavedFilter(
        label=label,
        slot=slot,
        params=params,
        active=True,
        position=position,
        fires_from=now,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def update(
    session: Session,
    filter_id: int,
    *,
    label: str,
    slot: int,
    params: Mapping[str, str],
    now: datetime,
) -> SavedFilter:
    """A new label, color or filters. Changed filters fire only for postings decided from
    ``now`` on; a new label or color changes nothing else."""
    row = _row(session, filter_id)
    label, params = _checked(session, label=label, slot=slot, params=params, editing=filter_id)
    if params != row.params:
        row.params = params  # a new dict, so the JSON column is saved
        row.fires_from = now
    row.label = label
    row.slot = slot
    row.updated_at = now
    session.flush()
    return row


def set_active(session: Session, filter_id: int, active: bool, *, now: datetime) -> None:
    """On or off. Switched on, it fires only for postings decided from ``now`` on."""
    row = _row(session, filter_id)
    if active and not row.active:
        row.fires_from = now
    row.active = active
    row.updated_at = now


def delete(session: Session, filter_id: int) -> None:
    session.delete(_row(session, filter_id))


def mark_viewed(session: Session, filter_id: int, *, now: datetime) -> None:
    _row(session, filter_id).last_viewed_at = now


def fitting(
    session: Session,
    filters: Sequence[Saved],
    posting_ids: Collection[int],
    *,
    now: datetime,
    status: str = "New",
    decided_after_save: bool = False,
) -> dict[int, list[Saved]]:
    """Posting id -> the active, valid saved filters it fits (by color), among
    ``posting_ids``, read in the ``status`` view. With ``decided_after_save``, a filter counts
    only postings decided since it last started firing (pushes)."""
    found: dict[int, list[Saved]] = {}
    if not posting_ids:
        return found
    for saved in sorted(filters, key=lambda item: item.row.slot):
        if not saved.usable or saved.query is None:
            continue
        ids = matching_ids(
            session,
            replace(saved.query, status=status),
            now=now,
            within=posting_ids,
            decided_since=saved.row.fires_from if decided_after_save else None,
        )
        for posting_id in ids:
            found.setdefault(posting_id, []).append(saved)
    return found


def new_match_counts(
    session: Session, filters: Sequence[Saved], *, now: datetime, hidden: Collection[str] = ()
) -> dict[int, int]:
    """Saved filter id -> its open, untriaged matches first seen since it was last opened
    (or saved, if it never was)."""
    counts: dict[int, int] = {}
    for saved in filters:
        if not saved.usable or saved.query is None:
            continue
        since = saved.row.last_viewed_at or saved.row.created_at
        counts[saved.row.id] = len(
            matching_ids(
                session,
                replace(saved.query, status="New", open_only=True),
                now=now,
                hidden=hidden,
                seen_since=since,
            )
        )
    return counts


def record_pushes(
    session: Session, posting_id: int, filter_ids: Collection[int], *, now: datetime
) -> None:
    """A posting's own message went out for these filters."""
    known = set(
        session.scalars(select(FilterPush.filter_id).where(FilterPush.posting_id == posting_id))
    )
    session.add_all(
        FilterPush(posting_id=posting_id, filter_id=filter_id, sent_at=now)
        for filter_id in sorted(set(filter_ids) - known)
    )


def pushed_since(session: Session, since: datetime) -> int:
    """How many postings got a message of their own since ``since``."""
    return (
        session.scalar(
            select(func.count(func.distinct(FilterPush.posting_id))).where(
                FilterPush.sent_at >= since
            )
        )
        or 0
    )


def color_blind(session: Session) -> bool:
    row = session.get(Preference, COLOR_BLIND)
    return bool(row and row.value)


def set_color_blind(session: Session, value: bool, *, now: datetime) -> None:
    row = session.get(Preference, COLOR_BLIND)
    if row is None:
        session.add(Preference(name=COLOR_BLIND, value=value, updated_at=now))
    else:
        row.value = value
        row.updated_at = now
