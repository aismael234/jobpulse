"""M5.1 through the cycle: with no saved filter every match goes out in one batch, as before;
with any saved filter only matches that fit an active one are pushed, one message each, with a
bullet per filter, most-matched last, spaced out, capped per cycle and per day with one
summary for the rest. A filter fires only for postings decided after it was saved."""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
import time_machine
from sqlalchemy import select

from jobpulse.config import NotifyConfig
from jobpulse.db import saved_filters
from jobpulse.db.tables import FilterPush, SavedFilter
from jobpulse.notify.base import Notification, NotifierError
from tests.factories import NOW, posting_row
from tests.integration.conftest import Env, GreenhouseMock, Web, gh_job

pytestmark = pytest.mark.respx(assert_all_called=False)

OLD = NOW - timedelta(days=30)
FRESH = NOW - timedelta(minutes=25)


async def baseline(env: Env, gh: GreenhouseMock) -> None:
    gh.respond(gh_job(100, published=OLD))
    await env.poll(sources=["greenhouse"])


def titles(env: Env) -> list[str]:
    return [message.title for message in env.notifier.sent]


def pushes(env: Env) -> dict[int, set[int]]:
    """posting id -> saved filter ids it was pushed for."""
    found: dict[int, set[int]] = {}
    with env.sessions() as session:
        for push in session.scalars(select(FilterPush)):
            found.setdefault(push.posting_id, set()).add(push.filter_id)
    return found


def notified(env: Env, external_id: str) -> bool:
    return env.posting("greenhouse", external_id).notified_at is not None


FOUR_JOBS = (
    gh_job(201, published=FRESH, location="Austin, TX"),  # fits both
    gh_job(202, "Platform Engineer", published=FRESH, location="Austin, TX"),  # Austin only
    gh_job(203, "Backend Engineer, Data", published=FRESH, location="Seattle, WA"),  # backend
    gh_job(204, "Platform Engineer, Infra", published=FRESH, location="Seattle, WA"),  # neither
)


async def test_with_no_saved_filter_a_cycle_still_sends_one_batch(
    env: Env, gh: GreenhouseMock
) -> None:
    await baseline(env, gh)
    gh.respond(gh_job(100, published=OLD), *FOUR_JOBS)
    await env.poll(sources=["greenhouse"])

    assert titles(env) == ["4 new job postings · watchlist"]
    assert env.sleep.calls == []


async def test_only_fitting_matches_go_out_one_each_most_matched_last(
    env: Env, gh: GreenhouseMock
) -> None:
    await baseline(env, gh)
    austin = env.save_filter("Austin", 0, place="US-TX")
    backend = env.save_filter("Backend", 1, q="backend")
    gh.respond(gh_job(100, published=OLD), *FOUR_JOBS)

    await env.poll(sources=["greenhouse"])

    sent = titles(env)
    assert len(sent) == 3
    # one filter each first, the lower score first (Platform's title scores less), then both
    assert sent[0].startswith("🟢 Acme · Platform Engineer · Austin")
    assert sent[1].startswith("🟣 Acme · Backend Engineer, Data · Seattle")
    assert sent[2].startswith("🟢🟣 Acme · Backend Engineer · Austin")
    assert env.sleep.calls == [5.0, 5.0]
    assert not notified(env, "204")  # fits no saved filter: in New, never pushed
    assert env.posting("greenhouse", "204").tier == "instant"
    ids = {external: env.posting("greenhouse", external).id for external in ("201", "202", "203")}
    assert pushes(env) == {
        ids["201"]: {austin, backend},
        ids["202"]: {austin},
        ids["203"]: {backend},
    }
    assert all(notified(env, external) for external in ("201", "202", "203"))


async def test_a_filter_never_fires_for_postings_decided_before_it(
    env: Env, gh: GreenhouseMock, time_machine: time_machine.TimeMachineFixture
) -> None:
    await baseline(env, gh)
    backend = env.save_filter("Backend", 0, q="backend")
    gh.respond(gh_job(100, published=OLD), gh_job(202, "Platform Engineer", published=FRESH))
    await env.poll(sources=["greenhouse"])
    assert env.notifier.sent == []

    time_machine.move_to(NOW + timedelta(minutes=20), tick=False)
    with env.sessions() as session:
        saved_filters.update(
            session,
            backend,
            label="Platform",
            slot=0,
            params={**session.get(SavedFilter, backend).params, "q": "platform"},  # type: ignore[union-attr]
            now=NOW + timedelta(minutes=20),
        )
        session.commit()
    gh.respond(
        gh_job(100, published=OLD),
        gh_job(202, "Platform Engineer", published=FRESH),
        gh_job(205, "Platform Engineer, Search", published=NOW + timedelta(minutes=15)),
    )
    await env.poll(sources=["greenhouse"])

    assert [title.split(" · ")[1] for title in titles(env)] == ["Platform Engineer, Search"]
    assert not notified(env, "202")


async def test_all_paused_is_silence_and_deleting_all_brings_the_batch_back(
    env: Env, gh: GreenhouseMock
) -> None:
    await baseline(env, gh)
    austin = env.save_filter("Austin", 0, place="US-TX")
    with env.sessions() as session:
        saved_filters.set_active(session, austin, False, now=NOW - timedelta(minutes=1))
        session.commit()
    gh.respond(gh_job(100, published=OLD), FOUR_JOBS[0])
    await env.poll(sources=["greenhouse"])
    assert env.notifier.sent == []
    assert not notified(env, "201")

    with env.sessions() as session:
        saved_filters.delete(session, austin)
        session.commit()
    await env.poll(sources=["greenhouse"])
    assert titles(env) == ["1 new job posting · watchlist"]
    assert notified(env, "201")


def every_backend_job(count: int) -> list[dict[str, Any]]:
    return [
        gh_job(
            300 + n,
            f"Backend Engineer, Team {'ABCDEFGH'[n]}",
            published=FRESH - timedelta(minutes=n),
        )
        for n in range(count)
    ]


async def test_over_the_cycle_cap_the_rest_go_out_as_one_summary_first(
    env: Env, gh: GreenhouseMock
) -> None:
    env.reconfigure(
        notify=NotifyConfig.model_validate({"channels": [], "saved_filters": {"max_per_cycle": 2}})
    )
    await baseline(env, gh)
    env.save_filter("Backend", 0, q="backend")
    jobs = every_backend_job(4)
    gh.respond(gh_job(100, published=OLD), *jobs)

    report = await env.poll(sources=["greenhouse"])

    sent = env.notifier.sent
    assert len(sent) == 3
    assert sent[0].body == "+2 more matching your saved filters: 🟢 2"
    assert all(message.title.startswith("🟢 Acme · Backend Engineer") for message in sent[1:])
    assert report.push_summary == sent[0].body
    assert env.sleep.calls == [5.0, 5.0]
    assert all(notified(env, str(job["id"])) for job in jobs)  # summarized ones too
    assert len(pushes(env)) == 2  # only messages of their own count toward the day


async def test_the_daily_cap_counts_the_last_24_hours(env: Env, gh: GreenhouseMock) -> None:
    env.reconfigure(
        notify=NotifyConfig.model_validate({"channels": [], "saved_filters": {"max_per_day": 3}})
    )
    await baseline(env, gh)
    env.save_filter("Backend", 0, q="backend")
    first, second = every_backend_job(2), every_backend_job(5)[2:]
    gh.respond(gh_job(100, published=OLD), *first)
    await env.poll(sources=["greenhouse"])
    assert len(env.notifier.sent) == 2

    gh.respond(gh_job(100, published=OLD), *first, *second)
    await env.poll(sources=["greenhouse"])

    later = env.notifier.sent[2:]
    assert [message.body for message in later[:1]] == ["+2 more matching your saved filters: 🟢 2"]
    assert len(later) == 2  # the summary, then the one message the day has left


class FailsOnCall:
    """A channel that refuses the ``nth`` message (1-based) and accepts the others."""

    name = "flaky"

    def __init__(self, nth: int) -> None:
        self.nth = nth
        self.calls = 0
        self.sent: list[Notification] = []

    async def send(self, message: Notification) -> None:
        self.calls += 1
        if self.calls == self.nth:
            raise NotifierError("channel down")
        self.sent.append(message)


async def test_a_failed_message_is_retried_next_cycle_and_a_sent_one_is_not(
    env: Env, gh: GreenhouseMock
) -> None:
    await baseline(env, gh)
    env.save_filter("Backend", 0, q="backend")
    env.notifier = FailsOnCall(2)
    jobs = every_backend_job(3)
    gh.respond(gh_job(100, published=OLD), *jobs)

    report = await env.poll(sources=["greenhouse"])

    assert len(env.notifier.sent) == 1
    assert report.failed_pushes == 2
    first = env.notifier.sent[0].title

    await env.poll(sources=["greenhouse"])
    assert len(env.notifier.sent) == 3
    assert [message.title for message in env.notifier.sent].count(first) == 1
    assert all(notified(env, str(job["id"])) for job in jobs)


async def test_an_invalid_saved_filter_is_skipped_never_widened(
    env: Env, gh: GreenhouseMock
) -> None:
    await baseline(env, gh)
    broken = env.save_filter("Old", 0, q="backend")
    with env.sessions() as session:
        row = session.get(SavedFilter, broken)
        assert row is not None
        row.params = {**row.params, "exp": "2-4"}
        session.commit()
    gh.respond(gh_job(100, published=OLD), FOUR_JOBS[0])

    await env.poll(sources=["greenhouse"])

    assert env.notifier.sent == []
    assert not notified(env, "201")


async def test_a_dry_run_reports_without_sending_sleeping_or_saving(
    env: Env, gh: GreenhouseMock
) -> None:
    await baseline(env, gh)
    env.save_filter("Austin", 0, place="US-TX")
    gh.respond(gh_job(100, published=OLD), *FOUR_JOBS[:2])

    report = await env.poll(sources=["greenhouse"], dry_run=True)

    assert len(report.pushes) == 2
    assert all(line.startswith("🟢 Acme · ") for line in report.pushes)
    assert env.notifier.sent == []
    assert env.sleep.calls == []
    assert pushes(env) == {}


# --- a push and a click on the same saved filter agree, for every filter -------------------

PAY = {"currency": "USD", "interval": "per-year-salary", "min": 150000, "max": 200000}
LOW_PAY = {"currency": "USD", "interval": "per-year-salary", "min": 90000, "max": 110000}


@pytest.mark.parametrize(
    "params",
    [
        {"place": "US-TX"},
        {"place": "metro:bay-area"},
        {"pay": "120"},
        {"pay": "120", "pay_listed": "1"},
        {"contract": "hide"},
        {"contract": "only"},
        {"max_applicants": "50"},
        {"reposted": "show"},
        {"exp": "0-2"},
        {"q": "python"},
        {"remote": "yes"},
        {"company": "beta"},
        {"min_score": "0.65"},
        {"open": "1"},
        {"source": "greenhouse"},
    ],
)
async def test_the_push_matches_the_dashboard(
    env: Env, gh: GreenhouseMock, make_web: Callable[..., Web], params: dict[str, str]
) -> None:
    rows = [
        posting_row(
            locations=["Austin, TX"],
            compensation=PAY,
            employment_type="contract",
            applicant_min=30,
            applicant_max=30,
            score=0.9,
            years_required=2,
        ),
        posting_row(
            locations=["Seattle, WA"],
            employment_type="full_time",
            repost_reason="requisition",
            score=0.6,
            is_remote=True,
            description_text="Python services",
        ),
        posting_row(
            locations=["San Francisco, CA"],
            compensation=LOW_PAY,
            employment_type="full_time",
            applicant_min=250,
            score=0.7,
            years_required=7,
            company_name="Beta",
        ),
        posting_row(locations=["Remote (US)"], is_remote=True, score=0.5, years_required=1),
    ]
    with env.sessions() as session:
        for row in rows:
            row.decided_at = NOW - timedelta(seconds=30)
            session.add(row)
        session.commit()
    web = make_web()
    shown = set(web.ids(**params))
    filter_id = env.save_filter("Check", 0, **params)
    gh.respond()

    await env.poll(sources=["greenhouse"])

    assert {posting for posting, ids in pushes(env).items() if filter_id in ids} == shown
