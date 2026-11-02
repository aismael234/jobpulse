"""Per-source schedules with jitter, run on APScheduler 3.x.

Each source is a one-shot DateTrigger job that schedules its own next run when it finishes,
so a slow run never overlaps the next one and every source keeps its own cadence. A watchdog
notices when a source stops finishing runs (APScheduler 3.11.3 can stop scheduling for good after an error), so the
worker can exit and be restarted.
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from jobpulse.config import InfraConfig
from jobpulse.core.timeparse import utcnow
from jobpulse.httpclient import Sleep

log = logging.getLogger(__name__)

MIN_DELAY_SECONDS = 1.0
WATCHDOG_FACTOR = 3.0
WATCHDOG_CHECK_SECONDS = 60.0

# A job may return how many seconds to wait before its next run (``None``: its cadence).
JobFunc = Callable[[], Awaitable[float | None]]


@dataclass(frozen=True, slots=True)
class Cadence:
    interval_seconds: float
    jitter_seconds: float = 0.0


def next_delay(cadence: Cadence, rng: random.Random) -> float:
    """Seconds until the next run: uniform in interval ± jitter, never under a second."""
    low = cadence.interval_seconds - cadence.jitter_seconds
    high = cadence.interval_seconds + cadence.jitter_seconds
    return max(MIN_DELAY_SECONDS, rng.uniform(low, high))


def cadences_from_config(config: InfraConfig) -> dict[str, Cadence]:
    schedules = config.schedules
    sections = {
        "watchlist": schedules.watchlist,
        "simplify": schedules.simplify,
        "job_boards": schedules.job_boards,
    }
    return {
        name: Cadence(section.every_minutes * 60, section.jitter_seconds)
        for name, section in sections.items()
    }


class Worker:
    def __init__(
        self,
        jobs: Mapping[str, tuple[Cadence, JobFunc]],
        *,
        rng: random.Random | None = None,
        scheduler: Any = None,
        clock: Callable[[], datetime] = utcnow,
        first_run_max_delay: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
        watchdog_factor: float = WATCHDOG_FACTOR,
    ) -> None:
        self._jobs = dict(jobs)
        self._rng = rng or random.Random()
        self._scheduler = scheduler
        self._clock = clock
        self._first_run_max_delay = first_run_max_delay
        self._monotonic = monotonic
        self._watchdog_factor = watchdog_factor
        self._stopping = False
        # Monotonic time of each job's last finished run (or of start()). Monotonic,
        # so a machine waking from sleep doesn't look like a stall.
        self._last_finished: dict[str, float] = {}

    @property
    def watchdog_limit(self) -> float | None:
        """Seconds a source may go without finishing a run: the watchdog factor times the
        longest interval (plus jitter). ``None`` when there are no jobs."""
        spans = [
            schedule.interval_seconds + schedule.jitter_seconds
            for schedule, _ in self._jobs.values()
        ]
        return self._watchdog_factor * max(spans) if spans else None

    def stalled(self) -> list[str]:
        """Jobs that haven't finished a run within ``watchdog_limit``."""
        limit = self.watchdog_limit
        if limit is None:
            return []
        now = self._monotonic()
        return [name for name, last in self._last_finished.items() if now - last > limit]

    async def watch(
        self, *, every: float = WATCHDOG_CHECK_SECONDS, sleep: Sleep = asyncio.sleep
    ) -> list[str]:
        """Check every ``every`` seconds; return the stalled jobs once there are any."""
        if self.watchdog_limit is None:  # no jobs: nothing can stall
            await asyncio.get_running_loop().create_future()
        while True:
            await sleep(every)
            stalled = self.stalled()
            if stalled:
                log.error(
                    "watchdog: no run of %s finished in the last %.0f min",
                    ", ".join(stalled),
                    (self.watchdog_limit or 0) / 60,
                )
                return stalled

    def start(self) -> None:
        """Schedule every job's first run after a short random stagger, then start.

        Call this from inside the running event loop; APScheduler 3.11 needs one.
        """
        if self._scheduler is None:
            self._scheduler = AsyncIOScheduler(timezone=UTC)
        started = self._monotonic()
        for name, (schedule, _) in self._jobs.items():
            self._last_finished[name] = started
            self._schedule(name, self._next_run(schedule, first=True))
        self._scheduler.start()

    def _next_run(self, schedule: Cadence, *, first: bool, hint: float | None = None) -> datetime:
        now = self._clock()
        if hint is not None:
            return now + timedelta(seconds=max(MIN_DELAY_SECONDS, hint))
        if first:
            stagger = min(self._first_run_max_delay, schedule.interval_seconds)
            return now + timedelta(seconds=self._rng.uniform(0.0, stagger))
        return now + timedelta(seconds=next_delay(schedule, self._rng))

    def _schedule(self, name: str, run_date: datetime) -> None:
        self._scheduler.add_job(
            self.run_job,
            trigger=DateTrigger(run_date=run_date),
            args=[name],
            id=name,
            name=name,
            replace_existing=True,
            misfire_grace_time=None,
            coalesce=True,
        )
        log.info("next %s run at %s", name, run_date.isoformat(timespec="seconds"))

    async def run_job(self, name: str) -> None:
        schedule, job = self._jobs[name]
        hint: float | None = None
        try:
            hint = await job()
        except Exception:
            # Never let an exception reach APScheduler: 3.11.3 can stop scheduling for good.
            log.exception("scheduled job %s failed", name)
        finally:
            self._last_finished[name] = self._monotonic()
            if not self._stopping:
                self._schedule(name, self._next_run(schedule, first=False, hint=hint))

    def shutdown(self) -> None:
        self._stopping = True
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
