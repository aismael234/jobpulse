"""Source health: alert once when a target keeps failing or a busy target goes quiet, and send
one recovery message when it's back.

Rules (``health`` in config.yaml), per (source, target):
- ``consecutive_failures`` failed runs in a row (3) open a "failing" incident. JobSpy board
  errors are failed runs too, and a 304 counts as a success.
- A busy target, whose successful runs in the last ``busy_window_days`` have a median
  ``fetched_count`` of at least ``busy_min``, opens a "silent" incident once all its
  successful runs have returned nothing for ``zero_hours``. Nothing is closed meanwhile
  (detection rule 6); this alert is how you hear about it.
A failing incident recovers on the next successful run, a silent one when postings come
back. A skipped search records no run, so it neither fails nor recovers.
"""

import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Literal, cast

from sqlalchemy.orm import Session

from jobpulse.config import HealthConfig
from jobpulse.db import repo
from jobpulse.db.tables import Run
from jobpulse.notify.base import Notification

log = logging.getLogger(__name__)

IncidentKind = Literal["failing", "silent"]


@dataclass(frozen=True, slots=True)
class RunSample:
    started_at: datetime
    ok: bool
    fetched_count: int = 0
    not_modified: bool = False
    error_kind: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class HealthRules:
    consecutive_failures: int = 3
    busy_min: int = 5
    busy_window: timedelta = timedelta(days=7)
    zero_window: timedelta = timedelta(hours=24)

    @classmethod
    def from_config(cls, config: HealthConfig) -> "HealthRules":
        return cls(
            consecutive_failures=config.consecutive_failures,
            busy_min=config.busy_min,
            busy_window=timedelta(days=config.busy_window_days),
            zero_window=timedelta(hours=config.zero_hours),
        )


@dataclass(frozen=True, slots=True)
class Assessment:
    failing_streak: int = 0
    failing_since: datetime | None = None  # start of the oldest failed run in the streak
    last_error: str | None = None
    last_error_kind: str | None = None
    silent_since: datetime | None = None  # set when the silent rule holds
    usual_count: float | None = None  # the busy-window median
    recovered_from_failing: bool = False  # the newest run succeeded
    recovered_from_silent: bool = False  # the newest successful run returned postings


def assess(runs: Sequence[RunSample], *, now: datetime, rules: HealthRules) -> Assessment:
    """Judge one target from its runs, newest first."""
    if not runs:
        return Assessment()
    streak = 0
    failing_since: datetime | None = None
    for run in runs:
        if run.ok:
            break
        streak += 1
        failing_since = run.started_at
    newest = runs[0]

    # The silent rule looks only at successful runs that brought a body (not 304s).
    window = [
        run
        for run in runs
        if run.ok and not run.not_modified and run.started_at >= now - rules.busy_window
    ]
    usual = float(median(run.fetched_count for run in window)) if window else None
    silent_since: datetime | None = None
    if window and window[0].fetched_count == 0 and usual is not None and usual >= rules.busy_min:
        zero_since = window[0].started_at
        for run in window:
            if run.fetched_count:
                break
            zero_since = run.started_at
        if now - zero_since >= rules.zero_window:
            silent_since = zero_since
    return Assessment(
        failing_streak=streak,
        failing_since=failing_since,
        last_error=newest.error if streak else None,
        last_error_kind=newest.error_kind if streak else None,
        silent_since=silent_since,
        usual_count=usual,
        recovered_from_failing=newest.ok,
        recovered_from_silent=bool(window) and window[0].fetched_count > 0,
    )


def _when(value: datetime) -> str:
    return f"{value:%Y-%m-%d %H:%M} UTC"


def _duration(value: timedelta) -> str:
    hours = max(0.0, value.total_seconds()) / 3600
    return f"{hours:.0f} h" if hours < 72 else f"{hours / 24:.0f} d"


def failing_alert(source: str, target: str, assessment: Assessment) -> tuple[str, str]:
    """Title and body for a failing incident."""
    since = _when(assessment.failing_since) if assessment.failing_since else "recently"
    body = (
        f"{assessment.failing_streak} runs in a row failed since {since}. "
        f"Last error ({assessment.last_error_kind}): {assessment.last_error}. "
        "Nothing is closed while it fails; you'll get one message when it recovers."
    )
    return f"jobpulse: {source}/{target} is failing", body


def silent_alert(
    source: str, target: str, assessment: Assessment, *, now: datetime
) -> tuple[str, str]:
    since = assessment.silent_since or now
    usual = assessment.usual_count or 0
    body = (
        f"It usually returns about {usual:.0f} postings, but every successful run since "
        f"{_when(since)} returned none. Nothing has been closed; check that the board or "
        "search still exists."
    )
    return f"jobpulse: {source}/{target} returned no postings for {_duration(now - since)}", body


def recovery_text(
    kind: IncidentKind, source: str, target: str, *, opened_at: datetime, now: datetime
) -> tuple[str, str]:
    if kind == "failing":
        return (
            f"jobpulse: {source}/{target} recovered",
            f"Runs succeed again after {_duration(now - opened_at)} of failures.",
        )
    return (
        f"jobpulse: {source}/{target} is returning postings again",
        f"Postings are back after {_duration(now - opened_at)} without any.",
    )


@dataclass(frozen=True, slots=True)
class DueMessage:
    incident_id: int
    recovery: bool
    notification: Notification


class HealthMonitor:
    """Opens and resolves ``health_incidents`` rows and says which messages are due.

    ``review`` is a synchronous database phase; the caller sends the messages and then
    calls ``mark_sent`` for each one that went out, so a failed send is retried next cycle.
    """

    def __init__(self, rules: HealthRules) -> None:
        self.rules = rules

    def review(
        self, session: Session, keys: Collection[tuple[str, str]], *, now: datetime
    ) -> list[DueMessage]:
        """Update the incidents of the targets in ``keys`` (they just ran), then return every
        message still due, including alerts whose earlier send failed."""
        for source, target in sorted(keys):
            self._review(session, source, target, now=now)
        due: list[DueMessage] = []
        for incident in repo.incidents_due(session):
            if incident.resolved_at is None:
                message = Notification(
                    title=incident.title, body=incident.detail, priority=4, tags=("warning",)
                )
            else:
                title, body = recovery_text(
                    cast(IncidentKind, incident.kind),
                    incident.source,
                    incident.target,
                    opened_at=incident.opened_at,
                    now=incident.resolved_at,
                )
                message = Notification(
                    title=title, body=body, priority=3, tags=("white_check_mark",)
                )
            due.append(DueMessage(incident.id, incident.resolved_at is not None, message))
        return due

    def mark_sent(self, session: Session, message: DueMessage, *, now: datetime) -> None:
        repo.mark_incident_sent(session, message.incident_id, recovery=message.recovery, now=now)

    def _review(self, session: Session, source: str, target: str, *, now: datetime) -> None:
        rules = self.rules
        # The whole busy window is read only when the newest counted run came back empty.
        counted = repo.latest_counted_run(session, source=source, target=target)
        if counted is not None and counted.fetched_count == 0:
            runs = repo.recent_runs(
                session, source=source, target=target, since=now - rules.busy_window
            )
        else:
            runs = repo.recent_runs(
                session, source=source, target=target, limit=rules.consecutive_failures + 1
            )
        assessment = assess([_sample(run) for run in runs], now=now, rules=rules)
        incidents = repo.open_incidents(session, source=source, target=target)

        failing = incidents.get("failing")
        if failing is None and assessment.failing_streak >= rules.consecutive_failures:
            title, body = failing_alert(source, target, assessment)
            repo.open_incident(
                session,
                source=source,
                target=target,
                kind="failing",
                opened_at=assessment.failing_since or now,
                title=title,
                detail=body,
            )
            log.warning("health: %s", title)
        elif failing is not None and assessment.recovered_from_failing:
            failing.resolved_at = now
            log.info("health: %s/%s recovered", source, target)

        silent = incidents.get("silent")
        if silent is None and assessment.silent_since is not None:
            title, body = silent_alert(source, target, assessment, now=now)
            repo.open_incident(
                session,
                source=source,
                target=target,
                kind="silent",
                opened_at=assessment.silent_since,
                title=title,
                detail=body,
            )
            log.warning("health: %s", title)
        elif silent is not None and assessment.recovered_from_silent:
            silent.resolved_at = now
            log.info("health: %s/%s is returning postings again", source, target)


def _sample(run: Run) -> RunSample:
    return RunSample(
        started_at=run.started_at,
        ok=run.ok,
        fetched_count=run.fetched_count,
        not_modified=run.not_modified,
        error_kind=run.error_kind,
        error=run.error,
    )
