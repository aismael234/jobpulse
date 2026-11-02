"""The profile: your preferences as one row of JSON (M6). Like ``repo``, everything here
leaves the transaction to the caller.

Every save bumps ``revision``. Readers (the poller, the dashboard) keep a compiled copy and
parse the JSON again only when the revision moves (``ProfileReader``).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpulse.config import Profile, ProfileError, parse_profile, profile_data
from jobpulse.db.tables import ProfileRow

PROFILE_ID = 1


class StaleProfile(Exception):
    """The profile changed after the form that edits it was loaded."""


@dataclass(frozen=True, slots=True)
class StoredProfile:
    profile: Profile | None  # None: the stored data no longer validates (``problem``)
    revision: int  # 0: nothing stored yet
    onboarded: bool  # False: the worker waits
    problem: str | None
    updated_at: datetime | None


NOTHING_STORED = StoredProfile(None, 0, False, None, None)


def revision(session: Session) -> int:
    return session.scalar(select(ProfileRow.revision).where(ProfileRow.id == PROFILE_ID)) or 0


def get(session: Session) -> StoredProfile:
    row = session.get(ProfileRow, PROFILE_ID)
    if row is None:
        return NOTHING_STORED
    try:
        profile, problem = parse_profile(dict(row.data or {})), None
    except ProfileError as exc:
        profile, problem = None, f"the stored profile is invalid: {exc}"
    return StoredProfile(
        profile, row.revision, row.onboarded_at is not None, problem, row.updated_at
    )


def save(
    session: Session,
    profile: Profile,
    *,
    now: datetime,
    expected_revision: int | None = None,
    onboarded: bool = True,
) -> int:
    """Store ``profile`` and return its revision. With ``expected_revision``, a profile that
    moved on since (another tab, the CLI) raises ``StaleProfile`` instead."""
    row = session.get(ProfileRow, PROFILE_ID)
    current = row.revision if row is not None else 0
    if expected_revision is not None and expected_revision != current:
        raise StaleProfile(
            f"the profile changed since this page was loaded (revision {current}, not "
            f"{expected_revision})"
        )
    data = profile_data(profile)
    if row is None:
        row = ProfileRow(
            id=PROFILE_ID,
            data=data,
            revision=1,
            onboarded_at=now if onboarded else None,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.data = data  # a new dict, so the JSON column sees the change
        row.revision = current + 1
        row.updated_at = now
        if onboarded and row.onboarded_at is None:
            row.onboarded_at = now
    session.flush()
    return row.revision


def scored_hash(session: Session) -> str | None:
    return session.scalar(select(ProfileRow.scored_hash).where(ProfileRow.id == PROFILE_ID))


def mark_scored(session: Session, digest: str) -> None:
    row = session.get(ProfileRow, PROFILE_ID)
    if row is not None:
        row.scored_hash = digest
        session.flush()


class ProfileReader:
    """The stored profile, parsed again only when its revision moves. A stored profile that
    no longer validates keeps the last good one, with ``problem`` saying why."""

    def __init__(self) -> None:
        self._last: StoredProfile = NOTHING_STORED
        self._good: Profile | None = None

    def read(self, session: Session) -> StoredProfile:
        last = self._last
        if last.revision and revision(session) == last.revision:
            return last
        stored = get(session)
        if stored.profile is not None:
            self._good = stored.profile
        elif stored.problem is not None and self._good is not None:
            stored = StoredProfile(
                self._good, stored.revision, stored.onboarded, stored.problem, stored.updated_at
            )
        self._last = stored
        return stored
