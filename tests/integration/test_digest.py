"""The digest: one message for the matches that didn't get their own push, each posting in at
most one digest, blocked companies and triaged postings left out."""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from jobpulse.config import AppConfig
from jobpulse.db.tables import PostingRow
from jobpulse.digest import Digest, DigestResult, dashboard_link
from jobpulse.notify.base import Notification, NotifierError
from tests.factories import NOW, posting_row
from tests.integration.conftest import RecordingNotifier, make_config

DASHBOARD = "https://jobpulse.tail1234.ts.net/"


def add(sessions: sessionmaker[Session], **fields: Any) -> int:
    with sessions() as session:
        row = posting_row(**fields)
        session.add(row)
        session.commit()
        return row.id


def digest_marks(sessions: sessionmaker[Session]) -> dict[str, Any]:
    with sessions() as session:
        return {
            row.title: row.digest_sent_at
            for row in session.scalars(select(PostingRow).order_by(PostingRow.id))
        }


def digest(
    sessions: sessionmaker[Session], notifier: Any, config: AppConfig | None = None
) -> Digest:
    return Digest(
        config=config or make_config(notify={"channels": [], "dashboard_url": DASHBOARD}),
        sessions=sessions,
        notifier=notifier,
        clock=lambda: NOW,
    )


async def test_digest_lists_untriaged_matches_best_first_with_a_dashboard_link(
    database: sessionmaker[Session],
) -> None:
    add(database, title="Platform Engineer", score=0.41, company_name="Beta")
    add(database, title="Backend Engineer", score=0.52)
    add(database, title="Pushed", tier="instant", score=0.9, notified_at=NOW - timedelta(hours=1))
    add(database, title="Baseline", tier="baseline", score=None)
    add(database, title="Filtered", tier="filtered", score=None)
    add(database, title="Stale", tier="stale", score=None)
    add(database, title="Pending", tier="pending", score=None)
    notifier = RecordingNotifier()

    result = await digest(database, notifier).run()

    assert result.sent
    assert result.count == 2
    [message] = notifier.sent
    assert message.title == "jobpulse digest · 2 new matches"
    lines = message.body.splitlines()
    assert lines[0] == ("1. Acme · Backend Engineer · SF · posted 2 h ago (score 0.52)")
    assert lines[2].startswith("2. Beta · Platform Engineer · SF · posted 2 h ago")
    assert lines[-1] == f"All of them on the dashboard: {DASHBOARD}"
    assert message.click_url == DASHBOARD
    marks = digest_marks(database)
    assert marks["Backend Engineer"] == NOW
    assert marks["Platform Engineer"] == NOW
    assert {marks[title] for title in ("Pushed", "Baseline", "Filtered", "Stale", "Pending")} == {
        None
    }


async def test_a_posting_appears_in_at_most_one_digest(database: sessionmaker[Session]) -> None:
    add(database, title="First")
    notifier = RecordingNotifier()
    runner = digest(database, notifier)

    assert (await runner.run()).count == 1
    second = await runner.run()
    assert second == DigestResult(count=0)
    add(database, title="Second")
    third = await runner.run()

    assert [m.title for m in notifier.sent] == [
        "jobpulse digest · 1 new match",
        "jobpulse digest · 1 new match",
    ]
    assert "First" in notifier.sent[0].body
    assert third.notification is not None
    assert "Second" in third.notification.body
    assert "First" not in third.notification.body


async def test_held_back_and_expired_instant_matches_join_but_live_pushes_do_not(
    database: sessionmaker[Session],
) -> None:
    add(
        database,
        title="Held back",
        tier="digest",
        tier_reason="held back: over notify.max_instant_per_cycle",
        score=0.8,
    )
    add(  # its push is still being retried by the poller (inside the 48 h window)
        database,
        title="Retrying",
        tier="instant",
        score=0.7,
        first_seen_at=NOW - timedelta(hours=10),
    )
    add(  # the window passed without a successful push
        database,
        title="Never pushed",
        tier="instant",
        score=0.6,
        first_seen_at=NOW - timedelta(days=3),
    )
    notifier = RecordingNotifier()

    result = await digest(database, notifier).run()

    assert result.count == 2
    body = notifier.sent[0].body
    assert body.index("Held back") < body.index("Never pushed")
    assert "Retrying" not in body
    assert digest_marks(database)["Retrying"] is None


async def test_closed_merged_triaged_and_blocked_postings_are_left_out(
    database: sessionmaker[Session],
) -> None:
    primary = add(database, title="Primary", status="Saved")
    add(database, title="Closed", closed_at=NOW - timedelta(hours=3))
    add(database, title="Merged copy", tier="merged", merged_into_id=primary)
    add(database, title="Dismissed", status="Dismissed")
    add(database, title="Applied", status="Applied")
    add(database, title="Agency job", company_name="Jobot, Inc.")
    config = make_config(
        notify={"channels": [], "dashboard_url": DASHBOARD},
        blocklist={"companies": ["Jobot"]},
    )
    notifier = RecordingNotifier()

    result = await digest(database, notifier, config).run()

    assert result == DigestResult(count=0)
    assert notifier.sent == []
    assert set(digest_marks(database).values()) == {None}


async def test_a_failed_send_releases_the_claim_so_the_next_digest_retries(
    database: sessionmaker[Session],
) -> None:
    add(database, title="Backend Engineer")
    notifier = RecordingNotifier()
    notifier.failures_left = 1
    runner = digest(database, notifier)

    failed = await runner.run()
    assert not failed.sent
    assert failed.error == "channel down"
    assert digest_marks(database)["Backend Engineer"] is None

    retried = await runner.run()
    assert retried.sent
    assert digest_marks(database)["Backend Engineer"] == NOW


class AdoptingThenFailingNotifier:
    """While the send is in flight, the poller links the claimed copy to a new ATS row (which
    inherits the claim); then the send fails."""

    name = "adopting"

    def __init__(self, sessions: sessionmaker[Session], copy_id: int, primary_id: int) -> None:
        self.sessions = sessions
        self.copy_id = copy_id
        self.primary_id = primary_id

    async def send(self, message: Notification) -> None:
        with self.sessions() as session:
            copy = session.get(PostingRow, self.copy_id)
            assert copy is not None
            assert copy.digest_sent_at is not None
            session.execute(
                update(PostingRow)
                .where(PostingRow.id == self.primary_id)
                .values(digest_sent_at=copy.digest_sent_at)
            )
            session.execute(
                update(PostingRow)
                .where(PostingRow.id == self.copy_id)
                .values(merged_into_id=self.primary_id, tier="merged")
            )
            session.commit()
        raise NotifierError("ntfy answered HTTP 502")


async def test_releasing_a_claim_also_clears_it_where_it_was_inherited(
    database: sessionmaker[Session],
) -> None:
    copy_id = add(database, title="LinkedIn copy", source="linkedin")
    primary_id = add(database, title="ATS row", tier="baseline", score=None)
    notifier = AdoptingThenFailingNotifier(database, copy_id, primary_id)

    result = await digest(database, notifier).run()

    assert not result.sent
    assert digest_marks(database) == {"LinkedIn copy": None, "ATS row": None}


async def test_a_dry_run_builds_the_message_but_marks_nothing(
    database: sessionmaker[Session],
) -> None:
    add(database, title="Backend Engineer")
    notifier = RecordingNotifier()

    result = await digest(database, notifier).run(dry_run=True)

    assert result.count == 1
    assert not result.sent
    assert result.notification is not None
    assert "Backend Engineer" in result.notification.body
    assert notifier.sent == []
    assert digest_marks(database)["Backend Engineer"] is None


async def test_without_a_channel_nothing_is_marked(database: sessionmaker[Session]) -> None:
    add(database, title="Backend Engineer")

    result = await digest(database, None).run()

    assert result.count == 1
    assert not result.sent
    assert result.error is not None
    assert digest_marks(database)["Backend Engineer"] is None


@pytest.mark.parametrize(
    ("dashboard", "expected"),
    [
        ({"dashboard_url": DASHBOARD}, DASHBOARD),
        ({}, "http://127.0.0.1:8000/"),
    ],
)
def test_dashboard_link(dashboard: dict[str, str], expected: str) -> None:
    config = make_config(notify={"channels": [], **dashboard})
    assert dashboard_link(config) == expected


def test_dashboard_link_uses_the_configured_local_address() -> None:
    assert (
        dashboard_link(make_config(dashboard={"host": "0.0.0.0", "port": 8080}))
        == "http://127.0.0.1:8080/"
    )
    assert (
        dashboard_link(make_config(dashboard={"host": "100.101.102.103"}))
        == "http://100.101.102.103:8000/"
    )
