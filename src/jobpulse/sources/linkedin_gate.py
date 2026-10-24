"""LinkedIn politeness, shared by every LinkedIn caller: JobSpy's searches and the public job
pages. One request at a time, a random pause of ``delay_min``–``delay_max`` seconds between
them, and after a 429 (or any other sign of a block) a cool-down that every caller respects.
"""

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from jobpulse.core.timeparse import utcnow

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Pacing:
    """A random pause between LinkedIn requests and a cool-down after a 429."""

    delay_min: float = 4.0
    delay_max: float = 8.0
    cooldown: timedelta = timedelta(minutes=30)


DEFAULT_PACING = Pacing()


class LinkedInGate:
    def __init__(
        self,
        pacing: Pacing = DEFAULT_PACING,
        *,
        clock: Callable[[], datetime] = utcnow,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.pacing = pacing
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._last: float | None = None  # monotonic end of the last request
        self._blocked_until: datetime | None = None

    def blocked_until(self) -> datetime | None:
        """When the current cool-down ends; ``None`` when there is none."""
        until = self._blocked_until
        if until is not None and self._clock() >= until:
            self._blocked_until = until = None
        return until

    @asynccontextmanager
    async def turn(self) -> AsyncIterator[None]:
        """Hold this for one request: it waits for the previous one's pause first."""
        async with self._lock:
            if self._last is not None:
                delay = self._rng.uniform(self.pacing.delay_min, self.pacing.delay_max)
                wait = delay - (self._monotonic() - self._last)
                if wait > 0:
                    await self._sleep(wait)
            try:
                yield
            finally:
                self._last = self._monotonic()

    def back_off(self) -> datetime:
        """Start the cool-down now; returns when it ends."""
        self._blocked_until = self._clock() + self.pacing.cooldown
        return self._blocked_until
