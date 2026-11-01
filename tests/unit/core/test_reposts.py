from datetime import timedelta

import pytest

from jobpulse.core.reposts import EarlierPosting, linkedin_repost, repost_reason, requisition
from tests.factories import NOW


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Reposted 3 hours ago", "Reposted 3 hours ago"),
        ("\n          Reposted\n   2 weeks ago\n        ", "Reposted 2 weeks ago"),
        ("reposted 1 day ago", "reposted 1 day ago"),
        ("3 hours ago", None),
        ("", None),
        (None, None),
    ],
)
def test_linkedin_marker(text: str | None, expected: str | None) -> None:
    assert linkedin_repost(text) == expected


def test_requisition_is_greenhouse_internal_job_id() -> None:
    assert requisition("greenhouse", {"internal_job_id": 1099719}) == "1099719"
    assert requisition("greenhouse", {"internal_job_id": None}) is None
    assert requisition("greenhouse", {}) is None
    assert requisition("lever", {"internal_job_id": 5}) is None


def test_linkedin_marker_wins() -> None:
    earlier = EarlierPosting(NOW - timedelta(days=12), requisition="1099719")
    assert (
        repost_reason(linkedin_marker="Reposted 2 days ago", earlier=earlier, first_seen_at=NOW)
        == "LinkedIn: Reposted 2 days ago"
    )


def test_an_earlier_posting_of_the_requisition() -> None:
    by_req = EarlierPosting(NOW - timedelta(days=12, hours=5), requisition="1099719")
    assert (
        repost_reason(linkedin_marker=None, earlier=by_req, first_seen_at=NOW)
        == "first seen 12 days ago (requisition 1099719)"
    )


def test_history_needs_seven_days() -> None:
    assert (
        repost_reason(
            linkedin_marker=None,
            earlier=EarlierPosting(NOW - timedelta(days=6, hours=23), requisition="7"),
            first_seen_at=NOW,
        )
        is None
    )
    assert repost_reason(
        linkedin_marker=None,
        earlier=EarlierPosting(NOW - timedelta(days=7), requisition="7"),
        first_seen_at=NOW,
    )
    assert repost_reason(linkedin_marker=None, earlier=None, first_seen_at=NOW) is None
