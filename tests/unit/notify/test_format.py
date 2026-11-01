from datetime import timedelta
from typing import Any

import pytest

from jobpulse.core.timeparse import start_of_day
from jobpulse.notify.format import (
    PushContent,
    batch_notification,
    category_label,
    humanize_age,
    posting_notification,
    saved_filter_summary,
    title_line,
    when_label,
)
from tests.factories import NOW


def content(**overrides: Any) -> PushContent:
    values: dict[str, Any] = {
        "company": "Stripe",
        "title": "Backend Engineer",
        "locations": ("San Francisco, CA", "Remote (US)"),
        "posted_at": NOW - timedelta(minutes=25),
        "precision": "exact",
        "first_seen_at": NOW - timedelta(minutes=2),
        "source": "greenhouse",
        "click_url": "https://stripe.com/jobs/search?gh_jid=8172487",
        "score": 0.82,
        "breakdown": {"freshness": 0.25, "title": 0.3, "stack": 0.17, "company_priority": 0.1},
    }
    values.update(overrides)
    return PushContent(**values)


def test_title_line_matches_the_spec_format() -> None:
    assert (
        title_line(content(), now=NOW)
        == "Stripe · Backend Engineer · SF / Remote · posted 25 min ago"
    )


def test_title_line_without_locations() -> None:
    assert (
        title_line(content(locations=()), now=NOW)
        == "Stripe · Backend Engineer · posted 25 min ago"
    )


def test_title_line_without_a_company() -> None:
    assert (
        title_line(content(company="", locations=()), now=NOW)
        == "Backend Engineer · posted 25 min ago"
    )


@pytest.mark.parametrize(
    ("age", "label"),
    [
        (timedelta(seconds=30), "just now"),
        (timedelta(minutes=1), "1 min ago"),
        (timedelta(minutes=59), "59 min ago"),
        (timedelta(minutes=60), "1 h ago"),
        (timedelta(hours=23, minutes=59), "23 h ago"),
        (timedelta(hours=24), "1 d ago"),
        (timedelta(days=3), "3 d ago"),
        (timedelta(seconds=-5), "just now"),
    ],
)
def test_humanize_age(age: timedelta, label: str) -> None:
    assert humanize_age(age) == label


def test_day_precision_labels() -> None:
    first_seen = NOW - timedelta(minutes=3)
    assert when_label(start_of_day(NOW), "day", first_seen, NOW) == "posted today"
    assert (
        when_label(start_of_day(NOW - timedelta(days=1)), "day", first_seen, NOW)
        == "posted yesterday"
    )
    assert (
        when_label(start_of_day(NOW - timedelta(days=3)), "day", first_seen, NOW)
        == "posted 3 d ago"
    )


def test_no_source_timestamp_uses_first_seen() -> None:
    assert when_label(None, "none", NOW - timedelta(minutes=5), NOW) == "first seen 5 min ago"


def test_a_batch_is_one_push_naming_the_count_the_job_and_the_top_score() -> None:
    matches = [
        content(company="Acme", score=0.61),
        content(company="Stripe", score=0.82, click_url="https://stripe.example/apply"),
        content(company="Initech", score=None),
    ]
    notification = batch_notification(
        matches, category="watchlist", priority=4, tags=("briefcase",)
    )
    assert notification.title == "3 new job postings · watchlist"
    assert notification.body == (
        "3 new job postings were found via watchlist. Top score: Stripe - 0.82"
    )
    assert notification.click_url == "https://stripe.example/apply"  # the top posting
    assert notification.priority == 4
    assert notification.tags == ("briefcase",)


def test_a_batch_of_one_is_singular() -> None:
    notification = batch_notification([content()], category="Simplify")
    assert notification.title == "1 new job posting · Simplify"
    assert notification.body == "1 new job posting was found via Simplify. Top score: Stripe - 0.82"


def test_a_batch_without_scores_or_a_company() -> None:
    notification = batch_notification([content(company="", score=None)], category="job boards")
    assert notification.body == "1 new job posting was found via job boards."
    assert notification.click_url == "https://stripe.com/jobs/search?gh_jid=8172487"


def test_an_empty_batch_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one match"):
        batch_notification([], category="watchlist")


@pytest.mark.parametrize(
    ("sources", "label"),
    [
        (("greenhouse", "lever", "ashby", "workday"), "watchlist"),
        (["greenhouse"], "watchlist"),
        (("simplify",), "Simplify"),
        (("indeed", "linkedin"), "job boards"),
        (["lever", "linkedin"], "watchlist and job boards"),
        (["lever", "simplify", "indeed"], "watchlist, Simplify and job boards"),
        (None, "all sources"),
    ],
)
def test_category_label(sources: tuple[str, ...] | list[str] | None, label: str) -> None:
    assert category_label(sources) == label


def test_a_posting_message_leads_with_its_bullets() -> None:
    """M5.1: one message per posting while saved filters exist."""
    message = posting_notification(content(), bullets="🟢🟣", now=NOW)
    assert message.title == "🟢🟣 Stripe · Backend Engineer · SF / Remote · 0.82"
    assert message.click_url == "https://stripe.com/jobs/search?gh_jid=8172487"
    assert "posted 25 min ago" in message.body
    assert "San Francisco, CA; Remote (US)" in message.body
    assert "greenhouse" in message.tags


def test_a_posting_message_without_a_score_or_place() -> None:
    message = posting_notification(content(score=None, locations=()), bullets="🟢", now=NOW)
    assert message.title == "🟢 Stripe · Backend Engineer"


def test_the_summary_counts_the_rest_per_filter() -> None:
    message = saved_filter_summary([("🟢", 4), ("🟣", 3)], total=7)
    assert message.title == "+7 more matching your saved filters"
    assert message.body == "+7 more matching your saved filters: 🟢 4 · 🟣 3"
    assert message.click_url is None
