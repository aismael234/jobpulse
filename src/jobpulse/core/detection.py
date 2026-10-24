"""The diff engine as pure functions: presence and closures (rule 6), the freshness gate
(rule 3) and the decision for a new posting. The repository applies what these return."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jobpulse.core.filters import FilterRules, check_description, check_locations, check_title
from jobpulse.core.models import Decision, FetchResult, Posting, Precision
from jobpulse.core.scoring import ScoreInputs, Weights, freshness_component, score
from jobpulse.core.timeparse import end_of_day


@dataclass(frozen=True, slots=True)
class PresenceRow:
    """What the presence plan needs to know about a stored posting."""

    id: int
    external_id: str
    closed_at: datetime | None = None
    missing_count: int = 0


@dataclass(frozen=True, slots=True)
class PresencePlan:
    seen_ids: tuple[int, ...] = ()  # present now: touch last_seen_at, reset missing_count
    reopened_ids: tuple[int, ...] = ()  # subset of seen_ids that were closed
    missing_counts: Mapping[int, int] = field(default_factory=dict)  # absent: new count
    closed_ids: tuple[int, ...] = ()  # absent for close_after qualifying polls


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationContext:
    now: datetime
    first_seen_at: datetime
    freshness: timedelta
    rules: FilterRules
    weights: Weights
    instant_threshold: float
    company_priority: float = 0.0
    applicant_signal: float | None = None


def can_drive_closures(result: FetchResult) -> bool:
    """Only a successful, complete, non-empty listing may count jobs as missing.

    A failure, an empty list (suspicious for a busy board) or any job-board search window
    never closes anything.
    """
    return result.ok and result.complete_listing and bool(result.postings)


def plan_presence(
    result: FetchResult, existing: Mapping[str, PresenceRow], *, close_after: int
) -> PresencePlan:
    """Compare a fetch with the stored postings of the same target.

    Present postings are seen (and reopened if closed). When the result can drive closures,
    each absent open posting's missing count goes up and closes at ``close_after``.
    """
    if not result.ok:
        return PresencePlan()
    present = {posting.external_id for posting in result.postings}
    drive_closures = can_drive_closures(result)
    seen: list[int] = []
    reopened: list[int] = []
    missing: dict[int, int] = {}
    closed: list[int] = []
    for external_id, row in existing.items():
        if external_id in present:
            seen.append(row.id)
            if row.closed_at is not None:
                reopened.append(row.id)
        elif drive_closures and row.closed_at is None:
            count = row.missing_count + 1
            missing[row.id] = count
            if count >= close_after:
                closed.append(row.id)
    return PresencePlan(tuple(seen), tuple(reopened), missing, tuple(closed))


def effective_posted_at(
    posted_at: datetime | None, precision: Precision, first_seen_at: datetime
) -> datetime:
    """The moment a posting's age is measured from.

    Exact timestamps are used as is. A day-precision date counts from the end of that day,
    but never later than ``first_seen_at``: a job posted today is never rejected, while
    "Posted 30+ Days Ago" still is. Without a timestamp, ``first_seen_at`` decides.
    """
    if posted_at is None or precision == "none":
        return first_seen_at
    if precision == "day":
        return min(end_of_day(posted_at), first_seen_at)
    return posted_at


def posting_age(posting: Posting, *, first_seen_at: datetime, now: datetime) -> timedelta:
    reference = effective_posted_at(posting.posted_at, posting.posted_at_precision, first_seen_at)
    return max(timedelta(0), now - reference)


def is_fresh(
    posting: Posting, *, first_seen_at: datetime, now: datetime, freshness: timedelta
) -> bool:
    return posting_age(posting, first_seen_at=first_seen_at, now=now) < freshness


def _duration(value: timedelta) -> str:
    hours = value.total_seconds() / 3600
    return f"{hours:.0f} h" if hours < 72 else f"{hours / 24:.0f} d"


def _stale(posting: Posting, ctx: EvaluationContext) -> Decision:
    age = posting_age(posting, first_seen_at=ctx.first_seen_at, now=ctx.now)
    return Decision(
        tier="stale",
        reason=f"{_duration(age)} old, outside the {_duration(ctx.freshness)} freshness window",
    )


def prescreen(posting: Posting, ctx: EvaluationContext) -> Decision | None:
    """Cheap checks before any detail fetch: the company blocklist, title, location, and
    freshness when the list already carries a timestamp. ``None`` means the posting
    survives."""
    if ctx.rules.blocklist.blocks_company(posting.company_name):
        return Decision(tier="filtered", reason="company is on the blocklist")
    title = check_title(posting.title, ctx.rules)
    if not title.passed:
        return Decision(tier="filtered", reason=title.reason)
    location = check_locations(posting.locations, ctx.rules.locations)
    if not location.passed:
        return Decision(tier="filtered", reason=location.reason)
    if posting.posted_at_precision != "none" and not is_fresh(
        posting, first_seen_at=ctx.first_seen_at, now=ctx.now, freshness=ctx.freshness
    ):
        return _stale(posting, ctx)
    return None


def evaluate(posting: Posting, ctx: EvaluationContext) -> Decision:
    """The full decision for a new (possibly enriched) posting."""
    early = prescreen(posting, ctx)
    if early is not None:
        return early
    if not is_fresh(posting, first_seen_at=ctx.first_seen_at, now=ctx.now, freshness=ctx.freshness):
        return _stale(posting, ctx)
    description = check_description(posting.description_text, ctx.rules)
    if not description.passed:
        return Decision(tier="filtered", reason=description.reason)
    title = check_title(posting.title, ctx.rules)
    age = posting_age(posting, first_seen_at=ctx.first_seen_at, now=ctx.now)
    total, breakdown = score(
        ScoreInputs(
            freshness=freshness_component(age, ctx.freshness),
            company_priority=ctx.company_priority,
            title=title.match,
            stack=description.stack_match,
            level_mismatch=description.level_mismatch,
            applicants=ctx.applicant_signal,
        ),
        ctx.weights,
    )
    instant = total >= ctx.instant_threshold
    reason = f"score {total:.2f} {'≥' if instant else '<'} {ctx.instant_threshold:.2f}"
    if description.level_mismatch:
        reason += f"; {description.reason}"
    return Decision(
        tier="instant" if instant else "digest", reason=reason, score=total, breakdown=breakdown
    )
