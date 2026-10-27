"""Template helpers: Jinja filters (ages, locations, pay, scores) and query-string links.

Filters that need the time or the location labels read ``now``, ``short_labels`` and ``zone``
from the template context, which every page render provides.
"""

import csv
import hashlib
import io
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import fields, replace
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from jinja2 import pass_context
from jinja2.runtime import Context

from jobpulse.core.atsurls import board_url
from jobpulse.core.dedupe import normalize_company
from jobpulse.core.locations import location_summary
from jobpulse.core.models import Precision
from jobpulse.core.pay import compensation_summary
from jobpulse.db.dashboard import PostingQuery
from jobpulse.notify.format import breakdown_text, humanize_age, when_label

_QUERY_PARAMS = {
    "status": "view",
    "source": "source",
    "company": "company",
    "remote": "remote",
    "min_score": "min_score",
    "text": "q",
    "open_only": "open",
    "place": "place",
    "pushed": "pushed",
    "min_pay": "pay",
    "pay_listed": "pay_listed",
    "experience": "exp",
    "sort": "sort",
    "page": "page",
}
_DEFAULT_QUERY = PostingQuery()


@pass_context
def ago(context: Context, value: datetime | None) -> str:
    """ "25 min ago", "3 h ago"; "never" for no time."""
    return humanize_age(context["now"] - value) if value is not None else "never"


@pass_context
def posted(context: Context, row: Any) -> str:
    """ "posted 25 min ago" from the source's timestamp, else "first seen 3 h ago"."""
    return when_label(
        row.posted_at, cast(Precision, row.posted_at_precision), row.first_seen_at, context["now"]
    )


@pass_context
def where(context: Context, locations: Sequence[str] | None) -> str:
    return location_summary(locations or (), short_labels=context["short_labels"])


@pass_context
def local(context: Context, value: datetime | None) -> str:
    """The time in the configured timezone, for tooltips."""
    if value is None:
        return ""
    return value.astimezone(context["zone"]).strftime("%Y-%m-%d %H:%M %Z")


def score(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "–"


def breakdown(value: Mapping[str, float | None] | None) -> str:
    return breakdown_text(value)


def pay(value: Mapping[str, Any] | None) -> str:
    return compensation_summary(value) or ""


def http_url(value: str | None) -> str | None:
    """The URL if it's http(s), else ``None``: feeds are third-party data, and an href must
    never run script (``javascript:`` and friends)."""
    if not value:
        return None
    try:
        scheme = urlsplit(value.strip()).scheme.lower()
    except ValueError:
        return None
    return value.strip() if scheme in ("http", "https") else None


def board(company: Any) -> str | None:
    return board_url(company.ats, company.token, company.region)


def link(query: PostingQuery, **changes: Any) -> str:
    """``/?view=saved&sort=fresh``: the query with ``changes``, defaults left out. Any change
    other than ``page`` starts over at page 1."""
    if changes and "page" not in changes:
        changes["page"] = 1
    updated = replace(query, **changes)
    params: list[tuple[str, str]] = []
    for item in fields(PostingQuery):
        name = _QUERY_PARAMS.get(item.name)
        value = getattr(updated, item.name)
        if name is None or value == getattr(_DEFAULT_QUERY, item.name) or value in (None, ""):
            continue
        if item.name == "status":
            value = str(value).lower()
        elif item.name == "remote":
            value = "yes" if value else "no"
        elif item.name in ("open_only", "pay_listed"):
            value = "1"
        params.append((name, str(value)))
    return "/?" + urlencode(params) if params else "/"


def export_url(query: PostingQuery) -> str:
    """The CSV of everything the query shows: ``/export.csv?view=applied``."""
    url = link(query, page=1)
    return "/export.csv" + url[1:]


def company_key(name: str | None) -> str:
    """A short id for a company, the same for "Acme, Inc." and "ACME", so a block can find
    that company's other cards on the page."""
    return hashlib.sha256(normalize_company(name or "").encode()).hexdigest()[:12]


# Third-party text opening with these becomes a formula in a spreadsheet.
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
CSV_COLUMNS = (
    "status",
    "moved",
    "company",
    "title",
    "locations",
    "source",
    "link",
    "posted",
    "pushed",
    "first seen",
    "score",
    "pay",
    "years asked",
    "closed",
)


def postings_csv(rows: Iterable[Any], *, zone: ZoneInfo, redact: Callable[[Any], Any]) -> str:
    """The rows as CSV, times in ``zone`` as on the cards. Cells pass through ``redact`` (the
    pages' secret mask) and never start like a formula."""

    def when(value: datetime | None) -> str:
        return value.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z") if value else ""

    def cell(value: Any) -> str:
        text = str(redact(value))
        return f"'{text}" if text.startswith(_FORMULA_STARTS) else text

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        values = (
            row.status,
            when(row.status_changed_at),
            row.company_name,
            row.title,
            "; ".join(row.locations or ()),
            row.source,
            http_url(row.apply_url or row.url) or "",
            # a day-precision date is stored at midnight UTC: the day, not a time
            f"{row.posted_at:%Y-%m-%d}"
            if row.posted_at and row.posted_at_precision == "day"
            else when(row.posted_at),
            when(row.notified_at),
            when(row.first_seen_at),
            "" if row.score is None else f"{row.score:.2f}",
            compensation_summary(row.compensation) or "",
            "" if row.years_required is None else row.years_required,
            when(row.closed_at),
        )
        writer.writerow([cell(value) for value in values])
    return out.getvalue()


FILTERS = {
    "ago": ago,
    "posted": posted,
    "where": where,
    "local": local,
    "score": score,
    "breakdown": breakdown,
    "pay": pay,
    "http_url": http_url,
    "board": board,
    "company_key": company_key,
}
