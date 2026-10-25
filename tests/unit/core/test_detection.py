"""Items 1, 3 and 7 (pure part): presence planning, closures and the freshness gate."""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from jobpulse.core.applicants import ApplicantCount
from jobpulse.core.detection import (
    EvaluationContext,
    PresenceRow,
    can_drive_closures,
    effective_posted_at,
    evaluate,
    is_fresh,
    plan_presence,
    posting_age,
    prescreen,
)
from jobpulse.core.filters import Blocklist, FilterRules, LocationRules
from jobpulse.core.models import FetchResult, Posting
from jobpulse.core.scoring import Weights
from jobpulse.core.timeparse import end_of_day, start_of_day
from tests.factories import NOW, compiled, make_posting, weighted

WINDOW = timedelta(hours=48)


def listing(*ids: str, complete: bool = True) -> FetchResult:
    postings = [make_posting(external_id=i, canonical_url=f"greenhouse:acme:{i}") for i in ids]
    return FetchResult.success(postings, fetched_at=NOW, complete_listing=complete)


EXISTING = {
    "a": PresenceRow(1, "a"),
    "b": PresenceRow(2, "b"),
    "c": PresenceRow(3, "c"),
}


# --- closures (rule 6) -------------------------------------------------------------


def test_only_complete_successful_nonempty_listings_drive_closures() -> None:
    assert can_drive_closures(listing("a"))
    assert not can_drive_closures(listing())
    assert not can_drive_closures(listing("a", complete=False))
    assert not can_drive_closures(FetchResult.failure("http_5xx", "boom", fetched_at=NOW))


def test_missing_job_is_counted_then_closed_on_the_second_miss() -> None:
    first = plan_presence(listing("a", "b"), EXISTING, close_after=2)
    assert first.seen_ids == (1, 2)
    assert first.missing_counts == {3: 1}
    assert first.closed_ids == ()

    after_one_miss = {**EXISTING, "c": PresenceRow(3, "c", missing_count=1)}
    second = plan_presence(listing("a", "b"), after_one_miss, close_after=2)
    assert second.missing_counts == {3: 2}
    assert second.closed_ids == (3,)


def test_reappearing_job_is_reopened() -> None:
    closed = {"c": PresenceRow(3, "c", closed_at=NOW - timedelta(days=1), missing_count=2)}
    plan = plan_presence(listing("c"), closed, close_after=2)
    assert plan.seen_ids == (3,)
    assert plan.reopened_ids == (3,)
    assert plan.closed_ids == ()
    assert plan.missing_counts == {}


def test_closed_job_still_missing_is_left_alone() -> None:
    closed = {
        **EXISTING,
        "c": PresenceRow(3, "c", closed_at=NOW - timedelta(days=1), missing_count=2),
    }
    plan = plan_presence(listing("a", "b"), closed, close_after=2)
    assert plan.missing_counts == {}
    assert plan.closed_ids == ()


@pytest.mark.parametrize(
    "result",
    [
        FetchResult.failure("http_5xx", "HTTP 500", fetched_at=NOW),
        FetchResult.failure("timeout", "timed out", fetched_at=NOW),
        FetchResult.failure("parse", "invalid JSON", fetched_at=NOW),
        FetchResult.success([], fetched_at=NOW, complete_listing=True),
    ],
    ids=["http-500", "timeout", "malformed-json", "empty-list"],
)
def test_failed_or_empty_results_close_nothing(result: FetchResult) -> None:
    plan = plan_presence(result, EXISTING, close_after=1)
    assert plan.missing_counts == {}
    assert plan.closed_ids == ()


def test_job_board_results_never_close_anything() -> None:
    plan = plan_presence(listing("a", complete=False), EXISTING, close_after=1)
    assert plan.seen_ids == (1,)
    assert plan.missing_counts == {}
    assert plan.closed_ids == ()


# --- freshness (rule 3) ------------------------------------------------------------


def test_exact_timestamps_are_used_as_is() -> None:
    assert effective_posted_at(NOW - timedelta(hours=2), "exact", NOW) == NOW - timedelta(hours=2)


def test_day_precision_today_is_never_older_than_first_seen() -> None:
    assert effective_posted_at(start_of_day(NOW), "day", NOW) == NOW


def test_day_precision_past_day_ends_at_the_end_of_that_day() -> None:
    posted = datetime(2026, 10, 20, tzinfo=UTC)
    assert effective_posted_at(posted, "day", NOW) == end_of_day(posted)


def test_no_timestamp_falls_back_to_first_seen() -> None:
    assert effective_posted_at(None, "none", NOW - timedelta(minutes=5)) == NOW - timedelta(
        minutes=5
    )


def check_fresh(posting: Posting, window: timedelta = WINDOW) -> bool:
    return is_fresh(posting, first_seen_at=NOW, now=NOW, freshness=window)


def test_freshness_gate() -> None:
    assert check_fresh(make_posting(posted_at=NOW - timedelta(hours=47)))
    assert not check_fresh(make_posting(posted_at=NOW - timedelta(days=5)))
    # Workday "Posted 30+ Days Ago" still fails the gate at day precision.
    thirty_days = start_of_day(NOW - timedelta(days=30))
    assert not check_fresh(make_posting(posted_at=thirty_days, posted_at_precision="day"))
    # "Posted Today" passes even with a one-hour window.
    today = make_posting(posted_at=start_of_day(NOW), posted_at_precision="day")
    assert check_fresh(today, window=timedelta(hours=1))
    # Without a source timestamp, first_seen_at decides.
    assert check_fresh(make_posting(posted_at=None))


def test_future_timestamps_count_as_brand_new() -> None:
    future = make_posting(posted_at=NOW + timedelta(hours=1))
    assert posting_age(future, first_seen_at=NOW, now=NOW) == timedelta(0)


# --- decisions -----------------------------------------------------------------------

RULES = FilterRules(
    title_include=weighted((r"\bbackend\b", 1.0)),
    title_exclude=compiled(r"\bmanager\b"),
    max_years=4,
    stack=weighted((r"\bpython\b", 1.0)),
    stack_saturation=1.0,
    locations=LocationRules(exclude=compiled(r"\bIndia\b")),
)


def context(**overrides: object) -> EvaluationContext:
    values: dict[str, object] = {
        "now": NOW,
        "first_seen_at": NOW,
        "freshness": WINDOW,
        "rules": RULES,
        "weights": Weights(),
        "company_priority": 0.5,
    }
    values.update(overrides)
    return EvaluationContext(**values)  # type: ignore[arg-type]


def test_prescreen_filters_on_title() -> None:
    decision = prescreen(make_posting(title="Engineering Manager, Backend"), context())
    assert decision is not None
    assert decision.tier == "filtered"


def test_prescreen_filters_on_location() -> None:
    decision = prescreen(make_posting(locations=("Bangalore, India",)), context())
    assert decision is not None
    assert decision.tier == "filtered"


def test_prescreen_filters_on_country() -> None:
    rules = dataclasses.replace(RULES, locations=LocationRules(countries=frozenset({"US"})))
    decision = prescreen(make_posting(locations=("Tokyo, Japan",)), context(rules=rules))
    assert decision is not None
    assert (decision.tier, decision.reason) == ("filtered", "no location in US (Tokyo, Japan)")
    assert prescreen(make_posting(locations=("Austin, TX",)), context(rules=rules)) is None


def test_prescreen_marks_known_old_postings_stale() -> None:
    decision = prescreen(make_posting(posted_at=NOW - timedelta(days=5)), context())
    assert decision is not None
    assert decision.tier == "stale"


def test_prescreen_filters_blocked_companies_first() -> None:
    rules = dataclasses.replace(RULES, blocklist=Blocklist.build(companies=["Acme Inc"]))
    decision = prescreen(make_posting(company_name="ACME, Inc."), context(rules=rules))
    assert decision is not None
    assert decision.tier == "filtered"
    assert decision.reason == "company is on the blocklist"
    assert prescreen(make_posting(company_name="Acme Widgets"), context(rules=rules)) is None
    stale = prescreen(
        make_posting(company_name="Acme", posted_at=NOW - timedelta(days=5)), context(rules=rules)
    )
    assert stale is not None
    assert stale.tier == "filtered"  # blocked, whatever else is wrong with it


def test_prescreen_passes_survivors_on() -> None:
    assert prescreen(make_posting(), context()) is None
    assert prescreen(make_posting(posted_at=None), context()) is None


def test_evaluate_instant() -> None:
    posting = make_posting(description_text="Python services. 3+ years of experience.")
    decision = evaluate(posting, context())
    assert decision.tier == "instant"
    assert decision.score is not None
    assert decision.score >= 0.55
    assert decision.breakdown is not None
    assert {"freshness", "company_priority", "title", "stack", "level_mismatch", "total"} <= set(
        decision.breakdown
    )


def test_evaluate_a_low_score_is_still_a_match() -> None:
    posting = make_posting(posted_at=NOW - timedelta(hours=40))
    decision = evaluate(posting, context(company_priority=0.0))
    assert decision.tier == "instant"
    assert decision.score is not None
    assert decision.score < 0.55
    assert decision.reason == f"score {decision.score:.2f}"


def test_evaluate_penalizes_too_many_years() -> None:
    posting = make_posting(description_text="Python. 6+ years of experience.")
    decision = evaluate(posting, context())
    assert decision.tier == "instant"
    assert decision.score is not None
    assert decision.breakdown is not None
    assert decision.breakdown["level_mismatch"] < 0
    assert "6" in decision.reason


def test_evaluate_excludes_too_many_years_in_exclude_mode() -> None:
    rules = FilterRules(title_include=RULES.title_include, max_years=4, years_mode="exclude")
    posting = make_posting(description_text="6+ years of experience.")
    assert evaluate(posting, context(rules=rules)).tier == "filtered"


def test_evaluate_stale() -> None:
    assert evaluate(make_posting(posted_at=NOW - timedelta(days=3)), context()).tier == "stale"


# --- M4: applicants and locations known only after enrichment -------------------------


def test_evaluate_scores_a_known_applicant_count() -> None:
    plain = evaluate(make_posting(), context())
    crowded = evaluate(
        make_posting(applicants=ApplicantCount("Over 200 applicants", 200, None)), context()
    )
    early = evaluate(
        make_posting(applicants=ApplicantCount("Be among the first 25 applicants", None, 25)),
        context(applicant_cap=100),
    )
    assert plain.breakdown is not None
    assert plain.breakdown["applicants"] is None
    assert crowded.breakdown is not None
    assert crowded.breakdown["applicants"] == 0.0
    assert early.breakdown is not None
    assert early.breakdown["applicants"] == pytest.approx(0.1 * 0.75)
    assert early.score == pytest.approx((plain.score or 0) + 0.075)


STRICT_LOCATIONS = dataclasses.replace(
    RULES, locations=LocationRules(include=compiled(r"Seattle"), unknown_passes=False)
)


def test_prescreen_leaves_a_missing_location_for_after_enrichment() -> None:
    """Workday's "2 Locations" means the places come with the detail call."""
    assert prescreen(make_posting(locations=()), context(rules=STRICT_LOCATIONS)) is None


def test_evaluate_judges_a_location_still_missing_after_enrichment() -> None:
    decision = evaluate(make_posting(locations=()), context(rules=STRICT_LOCATIONS))
    assert decision.tier == "filtered"
    assert "no location" in decision.reason


def test_evaluate_judges_locations_that_enrichment_supplied() -> None:
    near = evaluate(make_posting(locations=("Seattle, WA",)), context(rules=STRICT_LOCATIONS))
    far = evaluate(make_posting(locations=("Austin, TX",)), context(rules=STRICT_LOCATIONS))
    assert near.tier == "instant"
    assert far.tier == "filtered"
