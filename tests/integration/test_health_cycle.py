"""Item 12 end to end: a health alert after 3 failures, once per incident, then one recovery
message; and the busy-source-zero rule after 24 h of empty successes."""

from datetime import timedelta

import httpx
import pytest
import time_machine

from tests.factories import NOW, jobspy_frame
from tests.integration.conftest import Env, GreenhouseMock, ScriptedScrape, gh_job

pytestmark = pytest.mark.respx(assert_all_called=False)

OLD = NOW - timedelta(days=3)
FAILING = "jobpulse: greenhouse/acme is failing"
RECOVERED = "jobpulse: greenhouse/acme recovered"
SILENT = "jobpulse: greenhouse/acme returned no postings for 24 h"


def titles(env: Env) -> list[str]:
    return [message.title for message in env.notifier.sent]


async def test_three_failures_alert_once_then_one_recovery(env: Env, gh: GreenhouseMock) -> None:
    gh.respond(gh_job(101, published=OLD))
    await env.poll(sources=["greenhouse"])
    gh.fail_with(httpx.Response(503))
    await env.poll(sources=["greenhouse"])
    await env.poll(sources=["greenhouse"])
    assert titles(env) == []

    report = await env.poll(sources=["greenhouse"])
    assert titles(env) == [FAILING]
    assert report.health == [FAILING]
    body = env.notifier.sent[0].body
    assert "3 runs in a row" in body
    assert "http_5xx" in body

    await env.poll(sources=["greenhouse"])  # still failing: no second alert
    assert titles(env) == [FAILING]

    gh.respond(gh_job(101, published=OLD))
    await env.poll(sources=["greenhouse"])
    assert titles(env) == [FAILING, RECOVERED]
    await env.poll(sources=["greenhouse"])
    assert titles(env) == [FAILING, RECOVERED]

    [incident] = env.incidents()
    assert incident.kind == "failing"
    assert incident.alerted_at == NOW
    assert incident.resolved_at == NOW
    assert incident.recovery_sent_at == NOW


async def test_a_new_incident_after_recovery_alerts_again(env: Env, gh: GreenhouseMock) -> None:
    gh.respond(gh_job(101, published=OLD))
    await env.poll(sources=["greenhouse"])
    for _ in range(2):
        gh.fail_with(httpx.ReadTimeout("slow"))
        for _ in range(3):
            await env.poll(sources=["greenhouse"])
        gh.respond(gh_job(101, published=OLD))
        await env.poll(sources=["greenhouse"])
    assert titles(env) == [FAILING, RECOVERED, FAILING, RECOVERED]
    assert len(env.incidents()) == 2


async def test_an_alert_that_fails_to_send_is_retried(env: Env, gh: GreenhouseMock) -> None:
    gh.respond(gh_job(101, published=OLD))
    await env.poll(sources=["greenhouse"])
    gh.fail_with(httpx.Response(500))
    for _ in range(2):
        await env.poll(sources=["greenhouse"])
    env.notifier.failures_left = 1
    await env.poll(sources=["greenhouse"])
    assert titles(env) == []
    assert env.incidents()[0].alerted_at is None

    await env.poll(sources=["greenhouse"])
    assert titles(env) == [FAILING]


async def test_no_recovery_message_for_an_alert_nobody_got(env: Env, gh: GreenhouseMock) -> None:
    gh.respond(gh_job(101, published=OLD))
    await env.poll(sources=["greenhouse"])
    gh.fail_with(httpx.Response(500))
    env.notifier.failures_left = 3
    for _ in range(3):
        await env.poll(sources=["greenhouse"])
    gh.respond(gh_job(101, published=OLD))
    env.notifier.failures_left = 0
    await env.poll(sources=["greenhouse"])
    assert titles(env) == []
    [incident] = env.incidents()
    assert incident.resolved_at == NOW
    assert incident.alerted_at is None


async def test_job_board_errors_count_as_failures(env: Env, boards: ScriptedScrape) -> None:
    env.add_search()
    blocked = "unexpected page with no jobs (blocked?)"
    boards.queue(
        "linkedin",
        jobspy_frame([]),
        *(jobspy_frame([], errors={"linkedin": blocked}) for _ in range(3)),
    )
    for _ in range(4):
        await env.poll(sources=["linkedin"])
    assert titles(env) == ["jobpulse: linkedin/backend-sf is failing"]
    assert "jobspy_board_error" in env.notifier.sent[0].body


async def test_a_busy_board_silent_for_24_hours_alerts_once(
    env: Env, gh: GreenhouseMock, time_machine: time_machine.TimeMachineFixture
) -> None:
    time_machine.move_to(NOW - timedelta(days=7), tick=False)
    gh.respond(gh_job(101, published=OLD))
    await env.poll(sources=["greenhouse"])  # the baseline, a week ago
    time_machine.move_to(NOW, tick=False)
    half_hour = timedelta(minutes=30)
    env.add_runs(
        "greenhouse",
        "acme",
        [(NOW - timedelta(days=6) + step * half_hour, 120) for step in range(238)]
        + [(NOW - timedelta(hours=24, minutes=10) + step * half_hour, 0) for step in range(48)],
    )
    gh.respond()  # an empty list: suspicious for this board, so nothing is closed
    report = await env.poll(sources=["greenhouse"])

    assert titles(env) == [SILENT]
    assert report.health == [SILENT]
    assert "usually returns about 120" in env.notifier.sent[0].body
    assert env.posting("greenhouse", "101").closed_at is None

    await env.poll(sources=["greenhouse"])
    assert titles(env) == [SILENT]

    gh.respond(gh_job(101, published=OLD))
    await env.poll(sources=["greenhouse"])
    assert titles(env) == [SILENT, "jobpulse: greenhouse/acme is returning postings again"]


async def test_23_hours_of_empty_successes_is_not_yet_an_alert(
    env: Env, gh: GreenhouseMock, time_machine: time_machine.TimeMachineFixture
) -> None:
    time_machine.move_to(NOW - timedelta(days=7), tick=False)
    gh.respond(gh_job(101, published=OLD))
    await env.poll(sources=["greenhouse"])
    time_machine.move_to(NOW, tick=False)
    half_hour = timedelta(minutes=30)
    env.add_runs(
        "greenhouse",
        "acme",
        [(NOW - timedelta(days=6) + step * half_hour, 120) for step in range(240)]
        + [(NOW - timedelta(hours=23) + step * half_hour, 0) for step in range(46)],
    )
    gh.respond()
    await env.poll(sources=["greenhouse"])
    assert titles(env) == []


async def test_a_dry_run_reports_health_messages_without_sending(
    env: Env, gh: GreenhouseMock
) -> None:
    gh.respond(gh_job(101, published=OLD))
    await env.poll(sources=["greenhouse"])
    gh.fail_with(httpx.Response(503))
    for _ in range(2):
        await env.poll(sources=["greenhouse"])
    report = await env.poll(sources=["greenhouse"], dry_run=True)
    assert report.health == [FAILING]
    assert titles(env) == []
    assert env.incidents() == []


async def test_health_checks_can_be_turned_off(env: Env, gh: GreenhouseMock) -> None:
    env.reconfigure(health=env.config.health.model_copy(update={"enabled": False}))
    gh.respond(gh_job(101, published=OLD))
    await env.poll(sources=["greenhouse"])
    gh.fail_with(httpx.Response(503))
    for _ in range(4):
        await env.poll(sources=["greenhouse"])
    assert titles(env) == []
    assert env.incidents() == []
