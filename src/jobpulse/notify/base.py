"""The Notifier protocol, the Notification message, and fan-out to several channels."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class Notification:
    title: str
    body: str
    click_url: str | None = None
    priority: int = 3
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 1 <= self.priority <= 5:
            raise ValueError("priority must be between 1 and 5")


class NotifierError(Exception):
    """A channel did not accept the message. The text never contains secrets."""


class Notifier(Protocol):
    name: str

    async def send(self, message: Notification) -> None: ...


class FanOutNotifier:
    """Sends to every channel. Succeeds when at least one channel accepted the message."""

    name = "fan-out"

    def __init__(self, notifiers: Sequence[Notifier]) -> None:
        if not notifiers:
            raise ValueError("FanOutNotifier needs at least one channel")
        self.notifiers = list(notifiers)

    async def send(self, message: Notification) -> None:
        failures: list[str] = []
        for notifier in self.notifiers:
            try:
                await notifier.send(message)
            except NotifierError as exc:
                failures.append(f"{notifier.name}: {exc}")
        if len(failures) == len(self.notifiers):
            raise NotifierError("; ".join(failures))
        for failure in failures:
            log.warning("notification channel failed: %s", failure)
