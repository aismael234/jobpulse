"""Time helpers. Everything here is timezone-aware UTC.

Relative phrases ("3 days ago", Workday's "Posted 30+ Days Ago") are resolved against the
caller's ``now`` in UTC, because the sources don't say which timezone they count days in.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from jobpulse.core.models import Precision

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_EXTRA_FRACTION_DIGITS = re.compile(r"(\.\d{6})\d+")
_JUST_NOW = re.compile(r"(?:posted\s+)?(?:just now|moments? ago|a few seconds ago)", re.IGNORECASE)
_TODAY = re.compile(r"(?:posted\s+)?(?:today|just posted)", re.IGNORECASE)
_YESTERDAY = re.compile(r"(?:posted\s+)?yesterday", re.IGNORECASE)
_AGO = re.compile(
    r"(?:posted\s+)?(?P<count>\d+)\s*\+?\s*"
    r"(?P<unit>minutes?|mins?|hours?|hrs?|days?|weeks?|months?)\s+ago",
    re.IGNORECASE,
)
_UNIT_SECONDS = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600}
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30}


@dataclass(frozen=True, slots=True)
class ParsedTime:
    at: datetime
    precision: Precision


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"naive datetime {value.isoformat()} has no timezone")
    return value.astimezone(UTC)


def from_epoch_ms(value: float) -> datetime:
    """Lever's ``createdAt``. Integer arithmetic keeps the milliseconds exact."""
    return _EPOCH + timedelta(milliseconds=value)


def from_epoch_seconds(value: float) -> datetime:
    """SimplifyJobs' ``date_posted``."""
    return _EPOCH + timedelta(seconds=value)


def parse_iso(text: str) -> datetime:
    """ISO 8601 with ``Z``, offsets or any number of fractional digits. No offset means UTC."""
    cleaned = _EXTRA_FRACTION_DIGITS.sub(r"\1", text.strip())
    try:
        value = datetime.fromisoformat(cleaned)
    except ValueError:
        raise ValueError(f"not an ISO 8601 timestamp: {text!r}") from None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def start_of_day(value: datetime) -> datetime:
    return datetime.combine(ensure_utc(value).date(), time.min, tzinfo=UTC)


def end_of_day(value: datetime) -> datetime:
    return datetime.combine(ensure_utc(value).date(), time.max, tzinfo=UTC)


def parse_posted_ago(text: str, now: datetime) -> ParsedTime | None:
    """Minutes and hours ago are exact; days, weeks and months ago are day precision."""
    cleaned = " ".join(text.split()).rstrip(".")
    now = ensure_utc(now)
    if _JUST_NOW.fullmatch(cleaned):
        return ParsedTime(now, "exact")
    if _TODAY.fullmatch(cleaned):
        return ParsedTime(start_of_day(now), "day")
    if _YESTERDAY.fullmatch(cleaned):
        return ParsedTime(start_of_day(now - timedelta(days=1)), "day")
    match = _AGO.fullmatch(cleaned)
    if match is None:
        return None
    count = int(match["count"])
    unit = match["unit"].lower().removesuffix("s")
    if unit in _UNIT_SECONDS:
        return ParsedTime(now - timedelta(seconds=count * _UNIT_SECONDS[unit]), "exact")
    return ParsedTime(start_of_day(now - timedelta(days=count * _UNIT_DAYS[unit])), "day")


def parse_workday_posted_on(text: str, now: datetime) -> ParsedTime | None:
    """Workday's ``postedOn``: "Posted Today", "Posted Yesterday", "Posted 3 Days Ago",
    "Posted 30+ Days Ago". The "30+" bucket resolves to 30 days ago."""
    return parse_posted_ago(text, now)
