"""Logging with secret redaction.

httpx logs every request URL at INFO, and the ntfy topic and the Discord webhook token live
in URLs. So httpx and httpcore are pinned to WARNING, and every formatted line (message,
arguments and traceback) is scrubbed of known secret values.
"""

import logging
import sys
from collections.abc import Iterable
from typing import TextIO

REDACTED = "***"
MIN_SECRET_LENGTH = 4
QUIET_LOGGERS = ("httpx", "httpcore")
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: Iterable[str], fmt: str = LOG_FORMAT) -> None:
        super().__init__(fmt)
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


def configure_logging(
    level: str | int = "INFO", secrets: Iterable[str] = (), stream: TextIO | None = None
) -> logging.Handler:
    """Send all logs to one redacting stream handler (stderr by default)."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(RedactingFormatter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper() if isinstance(level, str) else level)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler
