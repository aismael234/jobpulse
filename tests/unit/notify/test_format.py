from datetime import timedelta
from typing import Any

import pytest

from jobpulse.core.timeparse import start_of_day
from jobpulse.notify.format import (
    PushContent,
    humanize_age,
    instant_notification,
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


def test_instant_notification() -> None:
    notification = instant_notification(content(), now=NOW, priority=4, tags=("briefcase",))
    assert notification.title == "Stripe · Backend Engineer · SF / Remote · posted 25 min ago"
    assert notification.click_url == "https://stripe.com/jobs/search?gh_jid=8172487"
    assert notification.priority == 4
    assert notification.tags == ("briefcase", "greenhouse")
    assert "Backend Engineer" in notification.body
    assert "San Francisco, CA" in notification.body
    assert "0.82" in notification.body
