"""Workday down for maintenance (2026-10-31): every board redirected to the maintenance page.
The cycle retries twice, pauses Workday for 30 minutes, and sends one message for the whole
source instead of one per board; one more when it's back. Boards of one source failing (or
recovering) together share one message too."""

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
import time_machine
from sqlalchemy import select

from jobpulse.db.tables import Run
from tests.factories import NOW
from tests.integration.conftest import Env

pytestmark = pytest.mark.respx(assert_all_called=False)

BOARDS = {
    "omega.wd5/External": "https://omega.wd5.myworkdayjobs.com/wday/cxs/omega/External/jobs",
    "zeta.wd1/Careers": "https://zeta.wd1.myworkdayjobs.com/wday/cxs/zeta/Careers/jobs",
}
MAINTENANCE_URL = "https://community.workday.com/maintenance-page"
DOWN = "jobpulse: workday is down for maintenance; pausing it for 30 min"
BACK = "jobpulse: workday is back from maintenance"


def jobs(request: httpx.Request) -> httpx.Response:
    company = request.url.host.split(".", 1)[0]
    job = {
        "title": f"{company} Backend Engineer",
        "externalPath": "/job/US-WA-Seattle/Backend-Engineer_R0000001",
        "locationsText": "US, WA, Seattle",
        "postedOn": "Posted 30+ Days Ago",
    }
    offset = json.loads(request.content)["offset"]
    return httpx.Response(200, json={"total": 1 if offset == 0 else 0, "jobPostings": [job]})


def maintenance(request: httpx.Request) -> httpx.Response:
    return httpx.Response(303, headers={"Location": MAINTENANCE_URL})


@pytest.fixture
def boards(env: Env, respx_mock: respx.MockRouter) -> list[respx.Route]:
    env.pause("acme", "beta")
    for token in BOARDS:
        env.watch(token.split(".")[0].title(), "workday", token)
    respx_mock.get(MAINTENANCE_URL).respond(200, html="<html>We'll be back soon</html>")
    routes = [respx_mock.post(url) for url in BOARDS.values()]
    answer(routes, jobs)
    return routes


def answer(routes: list[respx.Route], side_effect: Any) -> None:
    for route in routes:
        route.side_effect = side_effect


def titles(env: Env) -> list[str]:
    return [message.title for message in env.notifier.sent]


def runs(env: Env) -> list[Run]:
    with env.sessions() as session:
        return list(session.scalars(select(Run).order_by(Run.id)))


async def test_maintenance_retries_pauses_and_sends_one_message_each_way(
    env: Env, boards: list[respx.Route], time_machine: time_machine.TimeMachineFixture
) -> None:
    await env.poll(sources=["workday"])  # baseline
    answer(boards, maintenance)

    report = await env.poll(sources=["workday"])
    # respx answers without suspending, so the first board's three tries are over (and
    # Workday paused) before the second board starts: it's skipped without a request.
    first, second = report.targets
    assert (first.error_kind, first.skipped) == ("maintenance", False)
    assert (second.error_kind, second.skipped) == ("maintenance", True)
    assert [route.call_count for route in boards] == [1 + 3, 1]
    assert titles(env) == [DOWN]
    assert env.notifier.sent[0].body.startswith(
        "Its API redirects to its maintenance page, also after 2 retries."
    )
    [incident] = env.incidents()
    assert (incident.source, incident.target, incident.kind) == ("workday", "*", "maintenance")

    # Paused: every board is skipped, nothing is requested and no run is recorded.
    recorded = len(runs(env))
    report = await env.poll(sources=["workday"])
    assert all(target.skipped for target in report.targets)
    assert report.targets[0].error == "workday is under maintenance; paused until 14:30 UTC"
    assert [route.call_count for route in boards] == [4, 1]
    assert len(runs(env)) == recorded

    # Still down after each cool-down: more failed runs, but no per-board alerts.
    for step in (1, 2, 3):
        time_machine.move_to(NOW + timedelta(minutes=31 * step), tick=False)
        await env.poll(sources=["workday"])
    assert titles(env) == [DOWN]
    assert [run.error_kind for run in runs(env)][-4:] == ["maintenance"] * 4
    assert len(env.incidents()) == 1

    time_machine.move_to(NOW + timedelta(minutes=31 * 4), tick=False)
    answer(boards, jobs)
    report = await env.poll(sources=["workday"])
    assert all(target.ok for target in report.targets)
    assert titles(env) == [DOWN, BACK]
    assert env.notifier.sent[1].body == "It answers again after 2 h of maintenance."
    [incident] = env.incidents()
    assert incident.resolved_at == NOW + timedelta(minutes=124)


async def test_boards_of_one_source_failing_together_share_one_message(
    env: Env, boards: list[respx.Route]
) -> None:
    await env.poll(sources=["workday"])
    answer(boards, lambda request: httpx.Response(503))
    for _ in range(3):
        await env.poll(sources=["workday"])
    assert titles(env) == ["jobpulse: 2 workday boards are failing"]
    body = env.notifier.sent[0].body
    assert body.startswith("omega.wd5/External, zeta.wd1/Careers. workday/omega.wd5/External: ")
    assert "3 runs in a row failed" in body
    assert len(env.incidents()) == 2

    answer(boards, jobs)
    await env.poll(sources=["workday"])
    assert titles(env)[1:] == ["jobpulse: 2 workday boards recovered"]
    assert env.notifier.sent[1].body == "omega.wd5/External, zeta.wd1/Careers: runs succeed again."
    assert all(incident.recovery_sent_at == NOW for incident in env.incidents())


async def test_a_grouped_message_that_fails_to_send_is_retried_whole(
    env: Env, boards: list[respx.Route]
) -> None:
    await env.poll(sources=["workday"])
    answer(boards, lambda request: httpx.Response(503))
    for _ in range(2):
        await env.poll(sources=["workday"])
    env.notifier.failures_left = 1
    await env.poll(sources=["workday"])
    assert titles(env) == []
    assert all(incident.alerted_at is None for incident in env.incidents())

    await env.poll(sources=["workday"])
    assert titles(env) == ["jobpulse: 2 workday boards are failing"]
    assert all(incident.alerted_at == NOW for incident in env.incidents())
