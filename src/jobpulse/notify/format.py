"""Message formatting. Push title line:
``Stripe · Backend Engineer · SF / Remote · posted 25 min ago``."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from jobpulse.core.locations import DEFAULT_SHORT_LABELS, location_summary
from jobpulse.core.models import Precision
from jobpulse.core.timeparse import ensure_utc
from jobpulse.notify.base import Notification

SEPARATOR = " · "
_BREAKDOWN_LABELS = {
    "title": "title",
    "freshness": "fresh",
    "stack": "stack",
    "company_priority": "priority",
    "applicants": "applicants",
    "level_mismatch": "level",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class PushContent:
    company: str
    title: str
    locations: Sequence[str]
    posted_at: datetime | None
    precision: Precision
    first_seen_at: datetime
    source: str
    click_url: str | None
    score: float | None = None
    breakdown: Mapping[str, float | None] | None = None


def humanize_age(age: timedelta) -> str:
    seconds = max(0.0, age.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h ago"
    return f"{hours // 24} d ago"


def when_label(
    posted_at: datetime | None, precision: Precision, first_seen_at: datetime, now: datetime
) -> str:
    """ "posted 25 min ago" from the source's own timestamp, else "first seen ..."."""
    if posted_at is not None and precision == "exact":
        return f"posted {humanize_age(now - posted_at)}"
    if posted_at is not None and precision == "day":
        days = (ensure_utc(now).date() - ensure_utc(posted_at).date()).days
        if days <= 0:
            return "posted today"
        if days == 1:
            return "posted yesterday"
        return f"posted {days} d ago"
    return f"first seen {humanize_age(now - first_seen_at)}"


def title_line(
    content: PushContent, *, now: datetime, short_labels: Mapping[str, str] = DEFAULT_SHORT_LABELS
) -> str:
    parts = [content.company, content.title] if content.company else [content.title]
    where = location_summary(content.locations, short_labels=short_labels)
    if where:
        parts.append(where)
    parts.append(when_label(content.posted_at, content.precision, content.first_seen_at, now))
    return SEPARATOR.join(parts)


def _breakdown_text(breakdown: Mapping[str, float | None] | None) -> str:
    if not breakdown:
        return ""
    parts = [
        (label, value)
        for name, label in _BREAKDOWN_LABELS.items()
        if (value := breakdown.get(name)) is not None and value != 0
    ]
    parts.sort(key=lambda item: -abs(item[1]))
    return ", ".join(f"{label} {value:+.2f}" for label, value in parts)


def instant_notification(
    content: PushContent,
    *,
    now: datetime,
    priority: int = 4,
    tags: Sequence[str] = ("briefcase",),
    short_labels: Mapping[str, str] = DEFAULT_SHORT_LABELS,
) -> Notification:
    where = "; ".join(content.locations) or "location not given"
    lines = [content.title, f"{content.company}{SEPARATOR}{where}" if content.company else where]
    if content.score is not None:
        detail = _breakdown_text(content.breakdown)
        lines.append(
            f"{content.source}{SEPARATOR}score {content.score:.2f}"
            + (f" ({detail})" if detail else "")
        )
    return Notification(
        title=title_line(content, now=now, short_labels=short_labels),
        body="\n".join(lines),
        click_url=content.click_url,
        priority=priority,
        tags=tuple(dict.fromkeys((*tags, content.source))),
    )


def overflow_notification(count: int, *, dashboard_url: str | None = None) -> Notification:
    where = "the next digest and the dashboard" if dashboard_url else "the next digest"
    return Notification(
        title=f"jobpulse: {count} more matches this cycle",
        body=f"{count} more matching postings were held back to keep pushes down. They're in {where}.",
        click_url=dashboard_url,
        priority=3,
        tags=("briefcase",),
    )
