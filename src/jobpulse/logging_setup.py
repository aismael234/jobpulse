"""Logging with secret redaction.

httpx logs every request URL at INFO, and the ntfy topic and the Discord webhook token live
in URLs. So httpx and httpcore are pinned to WARNING, and every formatted line (message,
arguments and traceback) is scrubbed of known secret values.
"""

import logging
import sys
import time
from collections.abc import Iterable
from typing import TextIO

REDACTED = "***"
MIN_SECRET_LENGTH = 4
# httpx logs URLs (secrets); APScheduler logs every job run at INFO.
QUIET_LOGGERS = ("httpx", "httpcore", "apscheduler")
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: Iterable[str], fmt: str = LOG_FORMAT) -> None:
        super().__init__(fmt, datefmt=DATE_FORMAT)
        self.converter = time.gmtime  # timestamps in UTC, like everything else
        # Longest first, so a secret that contains another is replaced whole.
        self._secrets = sorted(
            {secret for secret in secrets if len(secret) >= MIN_SECRET_LENGTH},
            key=len,
            reverse=True,
        )

    def format(self, record: logging.LogRecord) -> str:
        return self.redact(super().format(record))

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text


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
    level: str | int = "INFO", secrets: Iterable[str] = (), stream: TextIO | None = None
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
