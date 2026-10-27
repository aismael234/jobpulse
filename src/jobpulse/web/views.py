"""Template helpers: Jinja filters (ages, locations, pay, scores) and query-string links.

Filters that need the time or the location labels read ``now``, ``short_labels`` and ``zone``
from the template context, which every page render provides.
"""

from collections.abc import Mapping, Sequence
from dataclasses import fields, replace
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlencode, urlsplit

from jinja2 import pass_context
from jinja2.runtime import Context

from jobpulse.core.atsurls import board_url
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
}
