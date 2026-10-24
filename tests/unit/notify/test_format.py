from datetime import timedelta
from typing import Any

import pytest

from jobpulse.core.timeparse import start_of_day
from jobpulse.notify.format import (
    DIGEST_BODY_BYTES,
    PushContent,
    digest_notification,
    humanize_age,
    instant_notification,
    overflow_notification,
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


def test_overflow_notification() -> None:
    notification = overflow_notification(7, dashboard_url="http://127.0.0.1:8000")
    assert "7 more" in notification.title
    assert notification.click_url == "http://127.0.0.1:8000"


# --- digest (Milestone 3) ---------------------------------------------------------------

DASHBOARD = "https://jobpulse.tail1234.ts.net/"


def test_digest_lists_numbered_title_lines_with_links_and_a_dashboard_link() -> None:
    items = [
        content(score=0.51),
        content(
            company="Figma",
            title="Platform Engineer",
            locations=("New York, NY",),
            posted_at=None,
            precision="none",
            first_seen_at=NOW - timedelta(hours=5),
            source="linkedin",
            click_url="https://www.linkedin.com/jobs/view/4101234567",
            score=0.42,
        ),
    ]
    message = digest_notification(items, now=NOW, dashboard_url=DASHBOARD)

    assert message.title == "jobpulse digest · 2 new matches"
    assert message.body.splitlines() == [
        "1. Stripe · Backend Engineer · SF / Remote · posted 25 min ago (score 0.51)",
        "   https://stripe.com/jobs/search?gh_jid=8172487",
        "2. Figma · Platform Engineer · NYC · first seen 5 h ago (score 0.42)",
        "   https://www.linkedin.com/jobs/view/4101234567",
        f"All of them on the dashboard: {DASHBOARD}",
    ]
    assert message.click_url == DASHBOARD
    assert message.priority == 3
    assert message.tags == ("newspaper",)


def test_digest_of_one_without_a_link_or_score() -> None:
    message = digest_notification(
        [content(score=None, click_url=None)], now=NOW, dashboard_url=None
    )
    assert message.title == "jobpulse digest · 1 new match"
    assert message.body == "1. Stripe · Backend Engineer · SF / Remote · posted 25 min ago"
    assert message.click_url is None


def test_a_long_digest_is_cut_to_fit_with_a_count_of_the_rest() -> None:
    items = [
        content(title=f"Backend Engineer {number}", click_url=f"https://example.com/jobs/{number}")
        for number in range(1, 201)
    ]
    message = digest_notification(items, now=NOW, dashboard_url=DASHBOARD)

    lines = message.body.splitlines()
    assert len(message.body.encode("utf-8")) <= DIGEST_BODY_BYTES
    shown = [line for line in lines if line[:1].isdigit()]
    assert shown[0].startswith("1. Stripe · Backend Engineer 1 ·")
    assert lines[-2] == f"+{200 - len(shown)} more"
    assert lines[-1] == f"All of them on the dashboard: {DASHBOARD}"
    assert message.title == "jobpulse digest · 200 new matches"


def test_digest_body_respects_a_smaller_budget() -> None:
    items = [content(title=f"Role {n}") for n in range(10)]
    message = digest_notification(items, now=NOW, dashboard_url=None, max_body_bytes=300)
    assert len(message.body.encode("utf-8")) <= 300
    assert message.body.splitlines()[-1].startswith("+")
