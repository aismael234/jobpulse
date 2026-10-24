"""Item 14: jitter stays within bounds, and every source keeps its own interval."""

import random
import statistics
from datetime import UTC, datetime, time
from types import SimpleNamespace
from typing import Any

import pytest

from jobpulse.config import AppConfig
from jobpulse.scheduler import (
    Cadence,
    DailyTimes,
    Worker,
    cadences_from_config,
    next_daily_run,
    next_delay,
)
from tests.factories import NOW, FakeMonotonic


def test_delay_stays_within_the_jitter_bounds() -> None:
    cadence = Cadence(interval_seconds=1800, jitter_seconds=120)
    rng = random.Random(42)
    samples = [next_delay(cadence, rng) for _ in range(10_000)]
    assert min(samples) >= 1680
    assert max(samples) <= 1920
    assert min(samples) < 1700  # the whole range is used
    assert max(samples) > 1900
    assert abs(statistics.mean(samples) - 1800) < 5


def test_zero_jitter_is_exact() -> None:
    assert next_delay(Cadence(interval_seconds=60), random.Random(1)) == 60


def test_delay_never_drops_below_the_minimum() -> None:
    cadence = Cadence(interval_seconds=0.5, jitter_seconds=0.4)
    assert all(next_delay(cadence, random.Random(i)) >= 1.0 for i in range(100))


def test_each_source_gets_its_own_cadence_from_config() -> None:
    config = AppConfig.model_validate(
        {
            "contact_email": "dev@example.com",
            "schedules": {
                "watchlist": {"every_minutes": 30, "jitter_seconds": 120},
                "simplify": {"every_minutes": 20, "jitter_seconds": 60},
            },
        }
    )
    cadences = cadences_from_config(config)
    assert cadences["watchlist"] == Cadence(1800, 120)
    assert cadences["simplify"] == Cadence(1200, 60)
    assert cadences["job_boards"] == Cadence(4500, 900)


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list[SimpleNamespace] = []
        self.started = False
        self.stopped = False

    def add_job(self, func: Any, trigger: Any, **kwargs: Any) -> None:
        self.jobs.append(SimpleNamespace(func=func, run_date=trigger.run_date, **kwargs))

    def start(self) -> None:
        self.started = True

    def shutdown(self, wait: bool = True) -> None:
        self.stopped = True

    def delay_of(self, job_id: str) -> float:
        job = [j for j in self.jobs if j.id == job_id][-1]
        run_date: datetime = job.run_date
        return (run_date - NOW).total_seconds()


async def test_worker_schedules_each_source_with_its_own_cadence() -> None:
    ran: list[str] = []

    async def poll_watchlist() -> None:
        ran.append("watchlist")

    async def poll_feed() -> None:
        ran.append("feed")

    scheduler = FakeScheduler()
    worker = Worker(
        {
            "watchlist": (Cadence(1800, 120), poll_watchlist),
            "feed": (Cadence(600, 30), poll_feed),
        },
        rng=random.Random(3),
        scheduler=scheduler,
        clock=lambda: NOW,
        first_run_max_delay=60,
    )
    worker.start()
    assert scheduler.started
    assert {job.id for job in scheduler.jobs} == {"watchlist", "feed"}
    for job_id in ("watchlist", "feed"):
        assert 0 <= scheduler.delay_of(job_id) <= 60

    await worker.run_job("watchlist")
    await worker.run_job("feed")
    assert ran == ["watchlist", "feed"]
    assert 1680 <= scheduler.delay_of("watchlist") <= 1920
    assert 570 <= scheduler.delay_of("feed") <= 630

    worker.shutdown()
    assert scheduler.stopped


async def test_a_failing_job_is_logged_and_still_rescheduled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def boom() -> None:
        raise RuntimeError("boom")

    scheduler = FakeScheduler()
    worker = Worker(
        {"watchlist": (Cadence(1800, 0), boom)},
        rng=random.Random(1),
        scheduler=scheduler,
        clock=lambda: NOW,
    )
    worker.start()
    await worker.run_job("watchlist")
    assert scheduler.delay_of("watchlist") == 1800
    assert "boom" in caplog.text


async def test_worker_jobs_follow_the_config() -> None:
    from jobpulse.runtime import scheduled_jobs

    config = AppConfig.model_validate(
        {
            "contact_email": "dev@example.com",
            "schedules": {"job_boards": {"every_minutes": 60, "jitter_seconds": 600}},
            "searches": [
                {"name": "backend-sf", "site": "linkedin", "search_term": "backend"},
                {"name": "old", "site": "indeed", "search_term": "x", "active": False},
            ],
        }
    )
    ran: list[tuple[str, tuple[str, ...]]] = []

    async def run(job: str, sources: tuple[str, ...]) -> None:
        ran.append((job, sources))

    jobs = scheduled_jobs(config, run)
    assert set(jobs) == {"watchlist", "simplify", "job_boards"}
    assert jobs["job_boards"][0] == Cadence(3600, 600)
    assert jobs["simplify"][0] == Cadence(1800, 120)
    for _, job in jobs.values():
        await job()
    assert ran == [
        ("watchlist", ("greenhouse", "lever", "ashby")),
        ("simplify", ("simplify",)),
        ("job_boards", ("indeed", "linkedin")),
    ]

    quiet = AppConfig.model_validate(
        {"contact_email": "dev@example.com", "simplify": {"enabled": False}}
    )
    assert set(scheduled_jobs(quiet, run)) == {"watchlist"}


# --- the digest's daily times (Milestone 3) -----------------------------------------------

DIGEST = DailyTimes((time(9, 0), time(18, 0)), "America/Los_Angeles")


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # 2026-10-23 is PDT (UTC-7): 09:00 local is 16:00 UTC, 18:00 local is 01:00 UTC next day
        (datetime(2026, 10, 23, 14, 0, tzinfo=UTC), datetime(2026, 10, 23, 16, 0, tzinfo=UTC)),
        (datetime(2026, 10, 23, 16, 0, tzinfo=UTC), datetime(2026, 10, 24, 1, 0, tzinfo=UTC)),
        (datetime(2026, 10, 23, 20, 30, tzinfo=UTC), datetime(2026, 10, 24, 1, 0, tzinfo=UTC)),
        (datetime(2026, 10, 24, 1, 0, 1, tzinfo=UTC), datetime(2026, 10, 24, 16, 0, tzinfo=UTC)),
        # DST ends on 2026-11-01: 09:00 PST is 17:00 UTC
        (datetime(2026, 11, 1, 2, 0, tzinfo=UTC), datetime(2026, 11, 1, 17, 0, tzinfo=UTC)),
    ],
)
def test_next_daily_run_is_the_next_local_time_in_utc(now: datetime, expected: datetime) -> None:
    assert next_daily_run(DIGEST, now) == expected


def test_next_daily_run_ignores_the_order_of_the_times() -> None:
    schedule = DailyTimes((time(18, 0), time(9, 0)), "UTC")
    assert next_daily_run(schedule, NOW) == datetime(2026, 10, 23, 18, 0, tzinfo=UTC)


async def test_a_daily_job_waits_for_its_time_and_reschedules_for_the_next() -> None:
    ran: list[str] = []

    async def digest() -> None:
        ran.append("digest")

    clock = {"now": NOW}  # 07:00 PDT
    scheduler = FakeScheduler()
    worker = Worker(
        {"digest": (DIGEST, digest)},
        rng=random.Random(1),
        scheduler=scheduler,
        clock=lambda: clock["now"],
    )
    worker.start()
    assert scheduler.jobs[-1].run_date == datetime(2026, 10, 23, 16, 0, tzinfo=UTC)

    clock["now"] = datetime(2026, 10, 23, 16, 0, 2, tzinfo=UTC)
    await worker.run_job("digest")
    assert ran == ["digest"]
    assert scheduler.jobs[-1].run_date == datetime(2026, 10, 24, 1, 0, tzinfo=UTC)


# --- the watchdog (Milestone 3) ----------------------------------------------------------


def watched_worker(monotonic: FakeMonotonic) -> tuple[Worker, list[str]]:
    ran: list[str] = []

    def job(name: str) -> Any:
        async def run() -> None:
            ran.append(name)

        return run

    worker = Worker(
        {
            "watchlist": (Cadence(1800, 120), job("watchlist")),
            "job_boards": (Cadence(4500, 900), job("job_boards")),
            "digest": (DIGEST, job("digest")),
        },
        rng=random.Random(2),
        scheduler=FakeScheduler(),
        clock=lambda: NOW,
        monotonic=monotonic,
    )
    return worker, ran


def test_watchdog_limit_is_three_times_the_longest_interval() -> None:
    worker, _ = watched_worker(FakeMonotonic())
    assert worker.watchdog_limit == 3 * (4500 + 900)


async def test_watchdog_flags_a_source_that_stopped_finishing_runs() -> None:
    monotonic = FakeMonotonic()
    worker, _ = watched_worker(monotonic)
    worker.start()
    assert worker.stalled() == []

    monotonic.now += 3 * 5400 - 1
    await worker.run_job("watchlist")  # the watchlist keeps going, job boards went quiet
    assert worker.stalled() == []
    monotonic.now += 2
    assert worker.stalled() == ["job_boards"]

    await worker.run_job("job_boards")
    assert worker.stalled() == []


async def test_watchdog_ignores_the_daily_digest() -> None:
    monotonic = FakeMonotonic()
    worker, _ = watched_worker(monotonic)
    worker.start()
    monotonic.now += 3 * 5400 - 10
    await worker.run_job("watchlist")
    await worker.run_job("job_boards")
    monotonic.now += 3 * 5400 - 10  # the digest never ran: fine
    assert worker.stalled() == []


async def test_watch_returns_the_stalled_jobs() -> None:
    monotonic = FakeMonotonic()
    worker, _ = watched_worker(monotonic)
    worker.start()
    checks: list[float] = []

    async def sleep(seconds: float) -> None:
        checks.append(seconds)
        monotonic.now += seconds

    stalled = await worker.watch(every=600, sleep=sleep)
    assert sorted(stalled) == ["job_boards", "watchlist"]
    assert len(checks) == 3 * 5400 // 600 + 1


def test_a_worker_without_interval_jobs_has_no_watchdog() -> None:
    async def digest() -> None:
        return None

    worker = Worker({"digest": (DIGEST, digest)}, scheduler=FakeScheduler(), clock=lambda: NOW)
    assert worker.watchdog_limit is None
    assert worker.stalled() == []


def test_digest_job_follows_the_config() -> None:
    from jobpulse.runtime import digest_job

    async def run() -> None:
        return None

    config = AppConfig.model_validate(
        {
            "contact_email": "dev@example.com",
            "timezone": "Europe/Berlin",
            "schedules": {"digest_times": ["18:30", "08:15"]},
        }
    )
    jobs = digest_job(config, run)
    assert list(jobs) == ["digest"]
    schedule, _ = jobs["digest"]
    assert schedule == DailyTimes((time(8, 15), time(18, 30)), "Europe/Berlin")
    # 18:30 in Berlin (CEST, UTC+2) on 2026-10-23
    assert next_daily_run(schedule, NOW) == datetime(2026, 10, 23, 16, 30, tzinfo=UTC)

    quiet = AppConfig.model_validate(
        {"contact_email": "dev@example.com", "schedules": {"digest_times": []}}
    )
    assert digest_job(quiet, run) == {}
