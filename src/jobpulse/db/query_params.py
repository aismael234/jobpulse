"""Dashboard filters as query-string parameters, both ways: ``parse_query`` reads a URL's (or
a saved filter's) parameters into a ``PostingQuery``, and ``filter_params`` writes a query's
filters out in full for a saved filter. The dashboard and the poller's saved-filter pushes
both read queries through here, so a push and a click on the same saved filter agree."""

import math
from collections.abc import Mapping
from typing import get_args

from jobpulse.core.models import SourceName
from jobpulse.core.places import parse_place_key
from jobpulse.db.dashboard import (
    CONTRACT_CHOICES,
    EXPERIENCE,
    EXPERIENCE_CHOICES,
    MAX_APPLICANTS_CHOICES,
    MOVED_SORT_LABELS,
    PUSHED_WINDOWS,
    STATUSES,
    ContractFilter,
    PostingQuery,
    SortOrder,
)

VIEWS = {status.lower(): status for status in STATUSES}
SORTS: dict[str, SortOrder] = {
    "score": "score",
    "fresh": "fresh",
    "pushed": "pushed",
    "moved": "moved",
    "resume": "resume",
}
CONTRACTS: dict[str, ContractFilter] = {value: value for value, _ in CONTRACT_CHOICES}
_SOURCES = frozenset(get_args(SourceName))


def _blank(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def parse_query(params: Mapping[str, str]) -> PostingQuery:
    """Filters from the query string. Blank or unreadable values are ignored, not errors."""
    status = VIEWS.get((params.get("view") or "").strip().lower(), "New")
    source = _blank(params.get("source"))
    remote = {"yes": True, "no": False}.get((params.get("remote") or "").strip().lower())
    min_score: float | None = None
    try:
        number = float(params.get("min_score") or "")
        if math.isfinite(number):
            min_score = max(0.0, number)
    except ValueError:
        pass
    try:
        page = max(1, int(params.get("page") or "1"))
    except ValueError:
        page = 1
    pushed = (params.get("pushed") or "").strip()
    experience = (params.get("exp") or "").strip()
    max_applicants = _thousands(params.get("max_applicants"))
    contract = CONTRACTS.get((params.get("contract") or "").strip().lower(), "show")
    sort = SORTS.get(params.get("sort") or "", "score")
    if sort == "moved" and status not in MOVED_SORT_LABELS:
        sort = "score"  # nothing is moved into New to sort by
    return PostingQuery(
        status=status,
        source=source if source in _SOURCES else None,
        company=_blank(params.get("company")),
        remote=remote,
        min_score=min_score,
        text=_blank(params.get("q")),
        open_only=_checked(params.get("open")),
        place=parse_place_key(params.get("place") or ""),
        pushed=pushed if pushed in PUSHED_WINDOWS else None,
        min_pay=_thousands(params.get("pay")),
        pay_listed=_checked(params.get("pay_listed")),
        experience=experience if experience in EXPERIENCE else None,
        show_reposted=(params.get("reposted") or "").strip().lower() == "show",
        max_applicants=max_applicants if max_applicants in MAX_APPLICANTS_CHOICES else None,
        contract=contract,
        sort=sort,
        page=page,
    )


def _checked(value: str | None) -> bool:
    return (value or "") in ("1", "on", "true", "yes")


def _thousands(value: str | None) -> int | None:
    """A pay floor in thousands of dollars: a positive whole number, else no floor."""
    try:
        number = int((value or "").strip())
    except ValueError:
        return None
    return number if number > 0 else None


# What a saved filter keeps: every filter, never the view, sort, page or "pushed within".
FILTER_KEYS: tuple[str, ...] = (
    "q",
    "place",
    "company",
    "source",
    "remote",
    "exp",
    "min_score",
    "pay",
    "pay_listed",
    "open",
    "reposted",
    "max_applicants",
    "contract",
)


def _number(value: float | int | None) -> str:
    if value is None:
        return ""
    return f"{value:g}" if isinstance(value, float) else str(value)


def filter_params(query: PostingQuery) -> dict[str, str]:
    """Every filter of ``query`` as parameters, defaults written out, so a saved filter keeps
    meaning what it meant when a default changes later."""
    return {
        "q": query.text or "",
        "place": query.place or "",
        "company": query.company or "",
        "source": query.source or "",
        "remote": {True: "yes", False: "no", None: ""}[query.remote],
        "exp": query.experience or "",
        "min_score": _number(query.min_score),
        "pay": _number(query.min_pay),
        "pay_listed": "1" if query.pay_listed else "0",
        "open": "1" if query.open_only else "0",
        "reposted": "show" if query.show_reposted else "hide",
        "max_applicants": _number(query.max_applicants),
        "contract": query.contract,
    }


def saved_query(params: object) -> tuple[PostingQuery | None, str | None]:
    """A saved filter's query (the New view), or why its parameters don't read back: a key
    missing or unknown, or a value the filters no longer accept. Never a wider query."""
    if not isinstance(params, Mapping):
        return None, "its filters aren't readable"
    if set(params) != set(FILTER_KEYS):
        unknown = sorted(str(key) for key in set(params) - set(FILTER_KEYS))
        missing = sorted(set(FILTER_KEYS) - set(params))
        detail = ", ".join(
            part
            for part in (
                f"unknown {', '.join(unknown)}" if unknown else "",
                f"missing {', '.join(missing)}" if missing else "",
            )
            if part
        )
        return None, f"its filters changed ({detail})"
    if not all(isinstance(value, str) for value in params.values()):
        return None, "its filters aren't readable"
    query = parse_query(params)
    changed = sorted(key for key, value in filter_params(query).items() if value != params[key])
    if changed:
        return None, f"{', '.join(changed)} no longer accept the saved value"
    return query, None


_REMOTE = {True: "Remote", False: "Not remote"}
_EXPERIENCE_LABELS = dict(EXPERIENCE_CHOICES)
_CONTRACT_LABELS = dict(CONTRACT_CHOICES)


def filter_summary(query: PostingQuery) -> str:
    """ "“python” · US-TX · Contract only": the filters that narrow, or "Every match"."""
    parts = [
        f"“{query.text}”" if query.text else "",
        query.place or "",
        f"company “{query.company}”" if query.company else "",
        query.source or "",
        _REMOTE.get(query.remote, "") if query.remote is not None else "",
        _EXPERIENCE_LABELS.get(query.experience or "", ""),
        f"under {query.max_applicants} applicants" if query.max_applicants else "",
        _CONTRACT_LABELS[query.contract] if query.contract != "show" else "",
        f"pay ≥ ${query.min_pay}k" if query.min_pay else "",
        "pay listed" if query.pay_listed else "",
        f"score ≥ {query.min_score:g}" if query.min_score is not None else "",
        "open only" if query.open_only else "",
        "reposts included" if query.show_reposted else "",
    ]
    return " · ".join(part for part in parts if part) or "Every match"
