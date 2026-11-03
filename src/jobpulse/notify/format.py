"""Message formatting. A cycle's matches go out as one push,
``3 new job postings were found via watchlist. Top score: Stripe - 0.87``; each match's title
line, ``Stripe · Backend Engineer · SF / Remote · posted 25 min ago``, is what the CLI and the
worker log show."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from jobpulse.core.locations import DEFAULT_SHORT_LABELS, location_summary
from jobpulse.core.models import ATS_SOURCES, FEED_SOURCES, JOB_BOARD_SOURCES, Precision
from jobpulse.core.timeparse import ensure_utc
from jobpulse.notify.base import Notification

SEPARATOR = " · "
_BREAKDOWN_LABELS = {
    "title": "title",
    "freshness": "fresh",
    "stack": "stack",
    "resume": "resume",
    "company_priority": "priority",
    "applicants": "applicants",
    "level_mismatch": "level",
    "llm": "llm",
}
# The worker's scheduled jobs, by the sources each one polls.
_CATEGORIES = (
    ("watchlist", ATS_SOURCES),
    ("Simplify", FEED_SOURCES),
    ("job boards", JOB_BOARD_SOURCES),
)


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


def breakdown_text(breakdown: Mapping[str, float | None] | None) -> str:
    """ "title +0.30, fresh +0.24, level -0.50": the score's parts, biggest first."""
    if not breakdown:
        return ""
    parts = [
        (label, value)
        for name, label in _BREAKDOWN_LABELS.items()
        if (value := breakdown.get(name)) is not None and value != 0
    ]
    parts.sort(key=lambda item: -abs(item[1]))
    return ", ".join(f"{label} {value:+.2f}" for label, value in parts)


def category_label(sources: Collection[str] | None) -> str:
    """The scheduled job a cycle stands for: "watchlist", "Simplify", "job boards", several of
    them, or "all sources"."""
    if sources is None:
        return "all sources"
    wanted = set(sources)
    names = [name for name, group in _CATEGORIES if group & wanted]
    if not names:
        return ", ".join(sorted(wanted))
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def batch_notification(
    matches: Sequence[PushContent],
    *,
    category: str,
    priority: int = 4,
    tags: Sequence[str] = ("briefcase",),
) -> Notification:
    """One push for a cycle's matches: "3 new job postings were found via watchlist. Top
    score: Stripe - 0.87". Tapping it opens the top posting."""
    if not matches:
        raise ValueError("a batch needs at least one match")
    count = len(matches)
    noun = "job posting" if count == 1 else "job postings"
    text = f"{count} new {noun} {'was' if count == 1 else 'were'} found via {category}."
    scored = [match for match in matches if match.score is not None]
    top = max(scored, key=lambda match: match.score or 0.0) if scored else matches[0]
    if top.score is not None:
        text += f" Top score: {top.company or top.title} - {top.score:.2f}"
    return Notification(
        title=f"{count} new {noun}{SEPARATOR}{category}",
        body=text,
        click_url=top.click_url,
        priority=priority,
        tags=tuple(tags),
    )


def posting_notification(
    content: PushContent,
    *,
    bullets: str,
    now: datetime,
    priority: int = 4,
    tags: Sequence[str] = ("briefcase",),
    short_labels: Mapping[str, str] = DEFAULT_SHORT_LABELS,
) -> Notification:
    """One posting's push while saved filters exist: "🟢🟣 Stripe · Backend Engineer · SF ·
    0.87", one bullet per saved filter it fits. Tapping it opens the posting."""
    parts = [content.company, content.title] if content.company else [content.title]
    where = location_summary(content.locations, short_labels=short_labels)
    if where:
        parts.append(where)
    if content.score is not None:
        parts.append(f"{content.score:.2f}")
    places = "; ".join(content.locations) or "location not given"
    lines = [
        content.title,
        f"{content.company}{SEPARATOR}{places}" if content.company else places,
        when_label(content.posted_at, content.precision, content.first_seen_at, now),
    ]
    if content.score is not None:
        detail = breakdown_text(content.breakdown)
        lines.append(
            f"{content.source}{SEPARATOR}score {content.score:.2f}"
            + (f" ({detail})" if detail else "")
        )
    return Notification(
        title=f"{bullets} {SEPARATOR.join(parts)}",
        body="\n".join(lines),
        click_url=content.click_url,
        priority=priority,
        tags=tuple(dict.fromkeys((*tags, content.source))),
    )


def saved_filter_summary(
    counts: Sequence[tuple[str, int]],
    *,
    total: int,
    priority: int = 4,
    tags: Sequence[str] = ("briefcase",),
) -> Notification:
    """The matches past a cycle's cap, in one message: "+7 more matching your saved filters:
    🟢 4 · 🟣 3" (a posting that fits two filters counts in both)."""
    title = f"+{total} more matching your saved filters"
    detail = SEPARATOR.join(f"{bullet} {count}" for bullet, count in counts)
    return Notification(title=title, body=f"{title}: {detail}", priority=priority, tags=tuple(tags))
