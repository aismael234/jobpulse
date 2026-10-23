"""Item 12 (rules): failure streaks and the busy-source-zero check, judged from run history."""

from datetime import timedelta

from jobpulse.health import (
    Assessment,
    HealthRules,
    RunSample,
    assess,
    failing_alert,
    recovery_text,
    silent_alert,
)
from tests.factories import NOW

RULES = HealthRules(consecutive_failures=3, busy_min=5)
HALF_HOUR = timedelta(minutes=30)


def ok(minutes_ago: float, count: int = 120, *, not_modified: bool = False) -> RunSample:
    return RunSample(
        started_at=NOW - timedelta(minutes=minutes_ago),
        ok=True,
        fetched_count=count,
        not_modified=not_modified,
    )


def failed(minutes_ago: float, kind: str = "http_5xx", error: str = "HTTP 503") -> RunSample:
    return RunSample(
        started_at=NOW - timedelta(minutes=minutes_ago), ok=False, error_kind=kind, error=error
    )


def history(*, zero_hours: float, normal: int = 120, days: float = 7) -> list[RunSample]:
    """Runs every 30 minutes, newest first: zeros for ``zero_hours``, ``normal`` before."""
    runs = []
    for step in range(int(days * 48)):
        minutes = step * 30
        runs.append(ok(minutes, 0 if minutes < zero_hours * 60 else normal))
    return runs


# --- failures ---------------------------------------------------------------------------


def test_three_failures_in_a_row_make_a_failing_streak() -> None:
    runs = [
        failed(0, "http_429", "linkedin: response status code 429"),
        failed(60),
        failed(120),
        ok(180),
    ]
    result = assess(runs, now=NOW, rules=RULES)
    assert result.failing_streak == 3
    assert result.failing_since == NOW - timedelta(minutes=120)
    assert result.last_error_kind == "http_429"
    assert result.last_error == "linkedin: response status code 429"
    assert not result.recovered_from_failing


def test_a_304_counts_as_a_success() -> None:
    result = assess([failed(0), ok(30, 0, not_modified=True), failed(60)], now=NOW, rules=RULES)
    assert result.failing_streak == 1


def test_a_success_on_top_recovers() -> None:
    result = assess([ok(0), failed(30), failed(60), failed(90)], now=NOW, rules=RULES)
    assert result.failing_streak == 0
    assert result.recovered_from_failing


def test_no_runs_no_verdict() -> None:
    assert assess([], now=NOW, rules=RULES) == Assessment()


# --- busy source returning zero ------------------------------------------------------------


def test_a_busy_target_silent_for_24_hours() -> None:
    result = assess(history(zero_hours=24.5), now=NOW, rules=RULES)
    assert result.silent_since is not None
    assert NOW - result.silent_since >= timedelta(hours=24)
    assert result.usual_count == 120
    assert not result.recovered_from_silent


def test_23_hours_of_zeros_is_not_yet_silent() -> None:
    assert assess(history(zero_hours=23), now=NOW, rules=RULES).silent_since is None


def test_a_quiet_target_is_never_silent() -> None:
    runs = history(zero_hours=30, normal=3)  # median below busy_min
    assert assess(runs, now=NOW, rules=RULES).silent_since is None


def test_failures_and_304s_do_not_break_or_extend_the_zero_streak() -> None:
    runs = history(zero_hours=25)
    runs[3] = failed(90)
    runs[5] = ok(150, 0, not_modified=True)
    assert assess(runs, now=NOW, rules=RULES).silent_since is not None


def test_postings_coming_back_recover_from_silence() -> None:
    runs = [ok(0, 87), *history(zero_hours=30)[1:]]
    result = assess(runs, now=NOW, rules=RULES)
    assert result.silent_since is None
    assert result.recovered_from_silent


def test_a_target_with_only_recent_history_is_not_judged_silent() -> None:
    runs = [ok(minutes, 0) for minutes in range(0, 26 * 60, 30)]  # all zero, all recent
    assert assess(runs, now=NOW, rules=RULES).silent_since is None


# --- messages -------------------------------------------------------------------------------


def test_failing_alert_names_the_target_and_the_last_error() -> None:
    assessment = assess(
        [failed(0, "http_429", "linkedin: response status code 429"), failed(60), failed(120)],
        now=NOW,
        rules=RULES,
    )
    title, body = failing_alert("linkedin", "backend-sf", assessment)
    assert title == "jobpulse: linkedin/backend-sf is failing"
    assert "3 runs in a row" in body
    assert "12:00 UTC" in body
    assert "http_429" in body
    assert "linkedin: response status code 429" in body


def test_silent_alert_gives_the_usual_count() -> None:
    assessment = assess(history(zero_hours=25), now=NOW, rules=RULES)
    title, body = silent_alert("greenhouse", "stripe", assessment, now=NOW)
    assert title == "jobpulse: greenhouse/stripe returned no postings for 24 h"
    assert "usually returns about 120" in body
    assert "Nothing has been closed" in body


def test_recovery_texts() -> None:
    opened = NOW - timedelta(hours=5)
    title, body = recovery_text("failing", "linkedin", "backend-sf", opened_at=opened, now=NOW)
    assert title == "jobpulse: linkedin/backend-sf recovered"
    assert "5 h" in body
    title, body = recovery_text("silent", "greenhouse", "stripe", opened_at=opened, now=NOW)
    assert title == "jobpulse: greenhouse/stripe is returning postings again"
