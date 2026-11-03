"""A source on its maintenance page: the redirect is a ``maintenance`` failure, retried twice,
then the whole source pauses for 30 minutes. Concurrent targets of one source retry one at a
time and give up once the maintenance is confirmed."""

import asyncio
from datetime import datetime, timedelta

import httpx
import respx

from jobpulse.core.models import CompanyTarget, FetchResult
from jobpulse.httpclient import HttpClient
from jobpulse.sources.maintenance import MaintenanceGate
from jobpulse.sources.workday import WorkdayAdapter
from tests.factories import NOW, FakeSleep

JOBS_URL = "https://omega.wd5.myworkdayjobs.com/wday/cxs/omega/External/jobs"
MAINTENANCE_URL = "https://community.workday.com/maintenance-page"
STATIC_URL = "https://static.community.workday.com/maintenance-page.html"
TARGET = CompanyTarget(
    company_id=7, ats="workday", token="omega.wd5/External", name="Omega", priority=2
)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def down() -> FetchResult:
    return FetchResult.failure("maintenance", "under maintenance", fetched_at=NOW)


def up() -> FetchResult:
    return FetchResult.success([], fetched_at=NOW, complete_listing=False)


class Script:
    """An attempt that answers from a list and counts its calls."""

    def __init__(self, *results: FetchResult) -> None:
        self.results = list(results)
        self.calls = 0

    async def __call__(self) -> FetchResult:
        self.calls += 1
        await asyncio.sleep(0)  # let the other targets of the source run
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def gate(
    clock: Clock, sleep: FakeSleep, *, retries: int = 2, cooldown: timedelta = timedelta(minutes=30)
) -> MaintenanceGate:
    return MaintenanceGate("workday", retries=retries, cooldown=cooldown, sleep=sleep, clock=clock)


async def test_retries_twice_then_pauses_the_source_for_30_minutes() -> None:
    clock, sleep = Clock(), FakeSleep()
    maintenance = gate(clock, sleep)
    attempt = Script(down())

    result = await maintenance.run(attempt)
    assert result.error_kind == "maintenance"
    assert not result.skipped  # attempted, so its run is recorded
    assert attempt.calls == 3
    assert sleep.calls == [30.0, 30.0]
    assert maintenance.paused_until == NOW + timedelta(minutes=30)

    skipped = await maintenance.run(attempt)
    assert skipped.skipped
    assert skipped.error_kind == "maintenance"
    assert skipped.error == "workday is under maintenance; paused until 14:30 UTC"
    assert attempt.calls == 3

    clock.now = NOW + timedelta(minutes=30)
    await maintenance.run(attempt)
    assert attempt.calls == 6
    assert maintenance.paused_until == NOW + timedelta(minutes=60)


async def test_an_answer_on_a_retry_ends_it_without_a_pause() -> None:
    clock, sleep = Clock(), FakeSleep()
    maintenance = gate(clock, sleep)
    attempt = Script(down(), up())

    result = await maintenance.run(attempt)
    assert result.ok
    assert attempt.calls == 2
    assert sleep.calls == [30.0]
    assert not maintenance.paused()


async def test_other_errors_are_not_retried_here() -> None:
    maintenance = gate(Clock(), FakeSleep())
    attempt = Script(FetchResult.failure("http_5xx", "HTTP 503", fetched_at=NOW))
    result = await maintenance.run(attempt)
    assert result.error_kind == "http_5xx"
    assert attempt.calls == 1
    assert not maintenance.paused()


async def test_concurrent_targets_retry_once_for_the_whole_source() -> None:
    clock, sleep = Clock(), FakeSleep()
    maintenance = gate(clock, sleep)
    attempts = [Script(down()) for _ in range(20)]

    results = await asyncio.gather(*(maintenance.run(attempt) for attempt in attempts))
    assert all(result.error_kind == "maintenance" for result in results)
    assert sum(attempt.calls for attempt in attempts) == 20 + 2
    assert sleep.calls == [30.0, 30.0]
    assert maintenance.paused()


async def test_targets_waiting_their_turn_retry_at_once_after_the_source_answered() -> None:
    clock, sleep = Clock(), FakeSleep()
    maintenance = gate(clock, sleep)
    attempts = [Script(down(), up()) for _ in range(5)]

    results = await asyncio.gather(*(maintenance.run(attempt) for attempt in attempts))
    assert all(result.ok for result in results)
    assert sleep.calls == [30.0]  # only the first one waited
    assert not maintenance.paused()


async def test_no_retries_pauses_after_the_first_attempt() -> None:
    clock, sleep = Clock(), FakeSleep()
    maintenance = gate(clock, sleep, retries=0, cooldown=timedelta(minutes=5))
    attempt = Script(down())
    await maintenance.run(attempt)
    assert attempt.calls == 1
    assert sleep.calls == []
    assert maintenance.paused_until == NOW + timedelta(minutes=5)


async def test_a_redirect_to_the_maintenance_page_is_a_maintenance_failure(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    """What Workday answered on 2026-10-31: a 303 to the maintenance page, which httpx follows
    as a GET to a static HTML page."""
    respx_mock.post(JOBS_URL).respond(303, headers={"Location": MAINTENANCE_URL})
    respx_mock.get(MAINTENANCE_URL).respond(302, headers={"Location": STATIC_URL})
    respx_mock.get(STATIC_URL).respond(200, html="<html>We'll be back soon</html>")

    result = await WorkdayAdapter(http, clock=lambda: NOW).fetch(TARGET)
    assert not result.ok
    assert result.error_kind == "maintenance"
    assert result.error == (
        "omega.wd5.myworkdayjobs.com is under maintenance "
        "(redirected to static.community.workday.com/maintenance-page.html)"
    )


async def test_the_adapter_retries_through_its_gate(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    route = respx_mock.post(JOBS_URL).respond(303, headers={"Location": MAINTENANCE_URL})
    respx_mock.get(MAINTENANCE_URL).respond(200, html="<html>maintenance</html>")
    clock, sleep = Clock(), FakeSleep()
    adapter = WorkdayAdapter(http, clock=clock, maintenance=gate(clock, sleep))

    assert (await adapter.fetch(TARGET)).error_kind == "maintenance"
    assert route.call_count == 3
    assert (await adapter.fetch(TARGET)).skipped
    assert route.call_count == 3

    clock.now = NOW + timedelta(minutes=31)
    route.respond(200, json={"total": 0, "jobPostings": []})
    assert (await adapter.fetch(TARGET)).ok


async def test_an_ordinary_redirect_is_followed(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    moved = "https://omega.wd5.myworkdayjobs.com/wday/cxs/omega/Careers/jobs"
    respx_mock.post(JOBS_URL).respond(307, headers={"Location": moved})
    respx_mock.post(moved).mock(return_value=httpx.Response(200, json={"jobPostings": []}))
    assert (await WorkdayAdapter(http, clock=lambda: NOW).fetch(TARGET)).ok
