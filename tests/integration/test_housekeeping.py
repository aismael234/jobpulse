"""Run history retention: the poller deletes runs older than ``retention.runs_days``, at most
once a day per process, and never in a dry run."""

from datetime import timedelta

import time_machine
from sqlalchemy import select

from jobpulse.db.tables import Run
from jobpulse.runtime import render_report
from tests.factories import NOW
from tests.integration.conftest import Env


def run_starts(env: Env) -> list[timedelta]:
    with env.sessions() as session:
        return sorted(NOW - started for started in session.scalars(select(Run.started_at)))


async def test_a_cycle_deletes_runs_older_than_the_window(env: Env) -> None:
    env.add_runs("greenhouse", "acme", [(NOW - timedelta(days=d), 5) for d in (45, 31, 29, 1)])

    report = await env.poll(sources=["linkedin"])  # no search configured: no fetches at all

    assert report.pruned_runs == 2
    assert run_starts(env) == [timedelta(days=1), timedelta(days=29)]
    assert "housekeeping: deleted 2 runs older than 30 days (retention.runs_days)" in (
        render_report(report)
    )


async def test_runs_are_pruned_at_most_once_a_day(
    env: Env, time_machine: time_machine.TimeMachineFixture
) -> None:
    poller = env.poller()
    env.add_runs("greenhouse", "acme", [(NOW - timedelta(days=40), 5)])
    assert (await poller.run_cycle(sources=["linkedin"])).pruned_runs == 1

    env.add_runs("greenhouse", "acme", [(NOW - timedelta(days=40), 5)])
    time_machine.move_to(NOW + timedelta(hours=23), tick=False)
    assert (await poller.run_cycle(sources=["linkedin"])).pruned_runs == 0
    time_machine.move_to(NOW + timedelta(hours=25), tick=False)
    assert (await poller.run_cycle(sources=["linkedin"])).pruned_runs == 1
    assert run_starts(env) == []


async def test_a_dry_run_deletes_nothing(env: Env) -> None:
    env.add_runs("greenhouse", "acme", [(NOW - timedelta(days=40), 5)])
    report = await env.poll(sources=["linkedin"], dry_run=True)
    assert report.pruned_runs == 0
    assert len(run_starts(env)) == 1
    assert not any("housekeeping" in line for line in render_report(report))


async def test_runs_can_be_kept_forever(env: Env) -> None:
    env.reconfigure(retention={"runs_days": None})
    env.add_runs("greenhouse", "acme", [(NOW - timedelta(days=400), 5)])
    report = await env.poll(sources=["linkedin"])
    assert report.pruned_runs == 0
    assert len(run_starts(env)) == 1
