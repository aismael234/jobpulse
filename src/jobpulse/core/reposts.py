"""Reposted postings: a job that was up before, under a new posting.

Two signals, either of which makes a posting a repost:

- **LinkedIn's own marker.** A bumped job's page reads "Reposted 3 hours ago" where a first
  posting reads "3 hours ago". LinkedIn's posted date is the repost date, so these pass the
  freshness check.
- **The Greenhouse requisition.** A Greenhouse posting whose requisition (``internal_job_id``,
  the company's own id for the opening, shared by every location of it) matches a posting
  jobpulse first saw on the same board at least ``REPOST_AFTER`` earlier, open or closed.

The same company, title and city is deliberately not a signal: companies hire several people
under one generic title, and a second opening is a new job. A posting under a URL jobpulse
already has is never new in the first place.

A repost still matches and is pushed like any other posting; the dashboard hides it by
default. These are pure rules: the history lookup is ``db.repo.earlier_posting``.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from jobpulse.core.text import collapse_whitespace

REPOST_AFTER = timedelta(days=7)
_LINKEDIN_MARKER = re.compile(r"\breposted\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class EarlierPosting:
    """The earliest other posting of the same Greenhouse requisition."""

    first_seen_at: datetime
    requisition: str


def linkedin_repost(text: str | None) -> str | None:
    """LinkedIn's "Reposted 3 hours ago" from a job page's posted-time text, or ``None`` for a
    first posting ("3 hours ago")."""
    if not text:
        return None
    text = collapse_whitespace(text)
    return text if _LINKEDIN_MARKER.match(text) else None


def requisition(source: str, raw: Mapping[str, Any]) -> str | None:
    """A Greenhouse job's requisition (``internal_job_id``): every location of one opening
    shares it. Other sources have nothing as reliable."""
    if source != "greenhouse":
        return None
    value = raw.get("internal_job_id")
    if value is None or isinstance(value, bool) or not isinstance(value, int | str):
        return None
    return str(value).strip() or None


def repost_reason(
    *, linkedin_marker: str | None, earlier: EarlierPosting | None, first_seen_at: datetime
) -> str | None:
    """Why a posting is a repost, or ``None``: LinkedIn's marker, else an earlier posting of
    the same requisition first seen at least ``REPOST_AFTER`` before this one."""
    if linkedin_marker:
        return f"LinkedIn: {linkedin_marker}"
    if earlier is None:
        return None
    age = first_seen_at - earlier.first_seen_at
    if age < REPOST_AFTER:
        return None
    return f"first seen {age.days} days ago (requisition {earlier.requisition})"
