"""Per-source schedules with jitter, run on APScheduler 3.x.

Each source is a one-shot DateTrigger job that schedules its own next run when it finishes,
so a slow run never overlaps the next one and every source keeps its own cadence.
"""

import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from jobpulse.config import AppConfig
from jobpulse.core.timeparse import utcnow

log = logging.getLogger(__name__)

MIN_DELAY_SECONDS = 1.0

JobFunc = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Cadence:
    interval_seconds: float
    jitter_seconds: float = 0.0


def next_delay(cadence: Cadence, rng: random.Random) -> float:
    """Seconds until the next run: uniform in interval ± jitter, never under a second."""
    low = cadence.interval_seconds - cadence.jitter_seconds
    high = cadence.interval_seconds + cadence.jitter_seconds
    return max(MIN_DELAY_SECONDS, rng.uniform(low, high))


def cadences_from_config(config: AppConfig) -> dict[str, Cadence]:
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
    ) -> None:
        self._jobs = dict(jobs)
        self._rng = rng or random.Random()
        self._scheduler = scheduler
        self._clock = clock
        self._first_run_max_delay = first_run_max_delay
        self._stopping = False

    def start(self) -> None:
        """Schedule every job's first run after a short random stagger, then start.

        Call this from inside the running event loop; APScheduler 3.11 needs one.
        """
        if self._scheduler is None:
            self._scheduler = AsyncIOScheduler(timezone=UTC)
        for name, (cadence, _) in self._jobs.items():
            stagger = min(self._first_run_max_delay, cadence.interval_seconds)
            self._schedule(name, self._rng.uniform(0.0, stagger))
        self._scheduler.start()

    def _schedule(self, name: str, delay: float) -> None:
        run_date = self._clock() + timedelta(seconds=delay)
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
        cadence, job = self._jobs[name]
        try:
            await job()
        except Exception:
            # Never let an exception reach APScheduler: 3.11.3 can stop scheduling for good.
            log.exception("scheduled job %s failed", name)
        finally:
            if not self._stopping:
                self._schedule(name, next_delay(cadence, self._rng))

    def shutdown(self) -> None:
        self._stopping = True
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
