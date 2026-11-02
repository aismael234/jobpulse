"""Logging with secret redaction.

httpx logs every request URL at INFO, and the ntfy topic and the Discord webhook token live
in URLs. So httpx and httpcore are pinned to WARNING, and every formatted line (message,
arguments and traceback) is scrubbed of known secret values.
"""

import logging
import sys
import threading
import time
from collections.abc import Iterable
from typing import TextIO

REDACTED = "***"
MIN_SECRET_LENGTH = 4
# httpx logs URLs (secrets), and so does httpx2 under the Anthropic SDK; APScheduler logs every
# job run at INFO.
QUIET_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2", "apscheduler")
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class SecretRegistry:
    """Every secret value this process knows, for redaction: ``.env``'s from the start, and
    those saved on Settings as soon as they're read or saved. Values are only ever added, so
    a secret replaced on Settings stays masked; reading takes no lock."""

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._lock = threading.Lock()
        self._values: tuple[str, ...] = ()
        self.add(values)

    def add(self, values: Iterable[str]) -> None:
        fresh = {value for value in values if len(value) >= MIN_SECRET_LENGTH}
        if fresh <= set(self._values):
            return
        with self._lock:
            # Longest first, so a secret that contains another is replaced whole.
            self._values = tuple(sorted(fresh | set(self._values), key=len, reverse=True))

    def values(self) -> tuple[str, ...]:
        return self._values

    def redact(self, text: str) -> str:
        for secret in self._values:
            text = text.replace(secret, REDACTED)
        return text


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: Iterable[str] | SecretRegistry, fmt: str = LOG_FORMAT) -> None:
        super().__init__(fmt, datefmt=DATE_FORMAT)
        self.converter = time.gmtime  # timestamps in UTC, like everything else
        self._registry = secrets if isinstance(secrets, SecretRegistry) else SecretRegistry(secrets)

    def format(self, record: logging.LogRecord) -> str:
        return self.redact(super().format(record))

    def redact(self, text: str) -> str:
        return self._registry.redact(text)


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stderr`` is when a record is emitted, so a swapped or closed
    stream (test runners, daemon wrappers) is never written to after the fact."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, value: TextIO) -> None:
        pass


_OWN_HANDLER = "_jobpulse_handler"


def configure_logging(
    level: str | int = "INFO",
    secrets: Iterable[str] | SecretRegistry = (),
    stream: TextIO | None = None,
) -> logging.Handler:
    """Install one redacting handler on the root logger (stderr by default).

    Calling it again replaces the previous jobpulse handler; handlers that something else
    installed are left alone.
    """
    handler: logging.Handler = (
        logging.StreamHandler(stream) if stream is not None else _StderrHandler()
    )
    handler.setFormatter(RedactingFormatter(secrets))
    setattr(handler, _OWN_HANDLER, True)
    root = logging.getLogger()
    root.handlers[:] = [h for h in root.handlers if not getattr(h, _OWN_HANDLER, False)] + [handler]
    root.setLevel(level.upper() if isinstance(level, str) else level)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler
