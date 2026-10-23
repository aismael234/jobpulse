"""Item 16: every source's timestamp format becomes an aware UTC datetime."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from jobpulse.core.timeparse import (
    ParsedTime,
    end_of_day,
    ensure_utc,
    from_epoch_ms,
    from_epoch_seconds,
    parse_iso,
    parse_posted_ago,
    parse_workday_posted_on,
    start_of_day,
)

NOW = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)


def day(year: int, month: int, day_: int) -> datetime:
    return datetime(year, month, day_, tzinfo=UTC)


def test_lever_epoch_milliseconds() -> None:
    parsed = from_epoch_ms(1786469891368)
    assert parsed == datetime(2026, 8, 11, 17, 38, 11, 368000, tzinfo=UTC)
    assert parsed.tzinfo is UTC


def test_simplify_epoch_seconds() -> None:
    assert from_epoch_seconds(1700000000) == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Greenhouse first_published
        ("2026-09-03T13:30:34-04:00", datetime(2026, 9, 3, 17, 30, 34, tzinfo=UTC)),
        # Ashby publishedAt
        ("2021-04-27T20:13:45.158+00:00", datetime(2021, 4, 27, 20, 13, 45, 158000, tzinfo=UTC)),
        ("2026-10-23T12:00:00Z", datetime(2026, 10, 23, 12, tzinfo=UTC)),
        ("2026-10-23T12:00:00.1234567Z", datetime(2026, 10, 23, 12, 0, 0, 123456, tzinfo=UTC)),
        ("2026-10-23T05:30:00+05:30", datetime(2026, 10, 23, 0, 0, tzinfo=UTC)),
        # A timestamp without an offset is taken as UTC.
        ("2026-10-23T12:00:00", datetime(2026, 10, 23, 12, tzinfo=UTC)),
    ],
)
def test_parse_iso(text: str, expected: datetime) -> None:
    parsed = parse_iso(text)
    assert parsed == expected
    assert parsed.tzinfo is UTC


def test_parse_iso_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="next tuesday"):
        parse_iso("next tuesday")


def test_ensure_utc() -> None:
    eastern = datetime(2026, 10, 23, 10, 0, tzinfo=timezone(timedelta(hours=-4)))
    converted = ensure_utc(eastern)
    assert converted == eastern
    assert converted.tzinfo is UTC
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(datetime(2026, 10, 23, 10, 0))  # noqa: DTZ001 - the point of the test


def test_day_bounds_are_in_utc() -> None:
    late_pacific = datetime(2026, 10, 20, 22, 0, tzinfo=timezone(timedelta(hours=-7)))
    assert start_of_day(late_pacific) == day(2026, 10, 21)
    assert end_of_day(datetime(2026, 10, 20, 5, tzinfo=UTC)) == datetime(
        2026, 10, 20, 23, 59, 59, 999999, tzinfo=UTC
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Posted Today", day(2026, 10, 23)),
        ("Posted Yesterday", day(2026, 10, 22)),
        ("Posted 3 Days Ago", day(2026, 10, 20)),
        ("Posted 30+ Days Ago", day(2026, 9, 23)),
        ("posted today", day(2026, 10, 23)),
    ],
)
def test_workday_posted_on(text: str, expected: datetime) -> None:
    assert parse_workday_posted_on(text, NOW) == ParsedTime(expected, "day")


def test_workday_posted_on_unknown_text() -> None:
    assert parse_workday_posted_on("Posted sometime", NOW) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("posted 25 minutes ago", ParsedTime(NOW - timedelta(minutes=25), "exact")),
        ("3 hours ago", ParsedTime(NOW - timedelta(hours=3), "exact")),
        ("Posted 1 hour ago", ParsedTime(NOW - timedelta(hours=1), "exact")),
        ("just now", ParsedTime(NOW, "exact")),
        ("2 days ago", ParsedTime(day(2026, 10, 21), "day")),
        ("30+ days ago", ParsedTime(day(2026, 9, 23), "day")),
        ("1 week ago", ParsedTime(day(2026, 10, 16), "day")),
        ("2 months ago", ParsedTime(day(2026, 8, 24), "day")),
    ],
)
def test_posted_ago(text: str, expected: ParsedTime) -> None:
    assert parse_posted_ago(text, NOW) == expected


def test_posted_ago_unknown_text() -> None:
    assert parse_posted_ago("a while back", NOW) is None
