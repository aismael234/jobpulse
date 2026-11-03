"""A source-wide pause for a source that's down for maintenance.

During Workday's maintenance (2026-10-31) every tenant's API answered with a 303 to
``community.workday.com/maintenance-page``. ``HttpAdapter`` reports such a redirect as a
``maintenance`` failure; the source's gate then retries the fetch ``retries`` times,
``retry_delay`` apart, and when the source is still under maintenance pauses the whole
source for ``cooldown``: its targets are skipped (no run recorded) until then.

The targets of a source fetch concurrently, so one of them retries at a time while the
others wait their turn. A target whose turn comes after the maintenance was confirmed gives
up at once; one whose turn comes after the source answered again retries without waiting.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from jobpulse.core.models import FetchResult
from jobpulse.core.timeparse import utcnow

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]


class MaintenanceGate:
    def __init__(
        self,
        name: str,
        *,
        retries: int = 2,
        retry_delay: timedelta = timedelta(seconds=30),
        cooldown: timedelta = timedelta(minutes=30),
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.name = name
        self.retries = retries
        self.retry_delay = retry_delay
        self.cooldown = cooldown
        self.paused_until: datetime | None = None
        self._sleep = sleep
        self._clock = clock
        self._lock = asyncio.Lock()
        self._answers = 0  # fetches that didn't land on a maintenance page

    def paused(self) -> bool:
        return self.paused_until is not None and self._clock() < self.paused_until

    def skip(self) -> FetchResult:
        """The result for a target of a paused source."""
        until = self.paused_until or self._clock()
        return FetchResult.skip(
            "maintenance",
            f"{self.name} is under maintenance; paused until {until:%H:%M} UTC",
            fetched_at=self._clock(),
        )

    async def run(self, attempt: Callable[[], Awaitable[FetchResult]]) -> FetchResult:
        """``attempt`` once, and while it lands on a maintenance page, up to ``retries``
        more times; then pause the source."""
        if self.paused():
            return self.skip()
        result = await attempt()
        if result.error_kind != "maintenance":
            self._answers += 1
            return result
        seen = self._answers
        async with self._lock:
            for _ in range(self.retries):
                if self.paused():  # another target of this source confirmed it meanwhile
                    return result
                if self._answers == seen:  # nothing answered since: wait before asking again
                    await self._sleep(self.retry_delay.total_seconds())
                seen = self._answers
                result = await attempt()
                if result.error_kind != "maintenance":
                    self._answers += 1
                    return result
            if not self.paused():
                self.paused_until = self._clock() + self.cooldown
                log.warning(
                    "%s is under maintenance after %d retries; paused until %s",
                    self.name, self.retries, f"{self.paused_until:%Y-%m-%d %H:%M} UTC",
                )  # fmt: skip
        return result
