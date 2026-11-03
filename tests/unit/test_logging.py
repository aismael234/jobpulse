import io
import logging
from collections.abc import Iterator

import pytest

from jobpulse.logging_setup import SecretRegistry, configure_logging

SECRET = "jobpulse-Zq3vX9kR2mT7wL4pB8nC1dF6"


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_secrets_are_redacted_from_messages_and_tracebacks() -> None:
    stream = io.StringIO()
    configure_logging("INFO", secrets=[SECRET], stream=stream)
    log = logging.getLogger("jobpulse.test")

    log.info("publishing to https://ntfy.sh/%s", SECRET)
    try:
        raise RuntimeError(f"request failed for https://ntfy.sh/{SECRET}")
    except RuntimeError:
        log.exception("send failed")

    output = stream.getvalue()
    assert SECRET not in output
    assert "https://ntfy.sh/***" in output
    assert "RuntimeError" in output


def test_httpx_request_logs_are_silenced() -> None:
    configure_logging("DEBUG", secrets=[], stream=io.StringIO())
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("apscheduler").getEffectiveLevel() == logging.WARNING


def test_short_or_empty_secrets_are_ignored() -> None:
    stream = io.StringIO()
    configure_logging("INFO", secrets=["", "ab"], stream=stream)
    logging.getLogger("jobpulse.test").info("about absent")
    assert "about absent" in stream.getvalue()


def test_the_registry_masks_secrets_added_later() -> None:
    registry = SecretRegistry(["early-secret"])
    stream = io.StringIO()
    handler = configure_logging("INFO", secrets=registry, stream=stream)
    try:
        registry.add(["late-secret-value", "abc"])  # too short to mask: never matched
        logging.getLogger("jobpulse.test").warning("early-secret late-secret-value abc")
    finally:
        logging.getLogger().removeHandler(handler)
    assert stream.getvalue().rstrip().endswith("*** *** abc")
    assert registry.values() == ("late-secret-value", "early-secret")
