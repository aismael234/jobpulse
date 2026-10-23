import asyncio
import random
from datetime import UTC, datetime

import httpx
import pytest
import respx

from jobpulse.httpclient import (
    REPLAY_HEADER,
    HttpClient,
    RetryPolicy,
    backoff_delay,
    build_user_agent,
    parse_retry_after,
)
from tests.factories import FakeSleep

URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
NOW = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)


def client(sleep: FakeSleep, **kwargs: object) -> HttpClient:
    return HttpClient(
        user_agent="jobpulse-test/0.1",
        sleep=sleep,
        rng=random.Random(7),
        clock=lambda: NOW,
        **kwargs,  # type: ignore[arg-type]
    )


def test_user_agent() -> None:
    assert (
        build_user_agent("dev@example.com")
        == "jobpulse/0.1.0 (+personal job alerts; contact: dev@example.com)"
    )
    assert build_user_agent(None) == "jobpulse/0.1.0 (+personal job alerts)"


@pytest.mark.parametrize("retry_number", range(8))
def test_backoff_is_full_jitter_under_the_cap(retry_number: int) -> None:
    policy = RetryPolicy(base_delay=1.0, max_delay=10.0)
    rng = random.Random(1)
    cap = min(10.0, 2.0**retry_number)
    samples = [backoff_delay(retry_number, policy, rng) for _ in range(500)]
    assert all(0.0 <= sample <= cap for sample in samples)
    assert max(samples) > cap * 0.8


def test_parse_retry_after() -> None:
    assert parse_retry_after("7", now=NOW) == 7.0
    assert parse_retry_after("2.5", now=NOW) == 2.5
    assert parse_retry_after("Fri, 23 Oct 2026 14:00:30 GMT", now=NOW) == 30.0
    assert parse_retry_after("Fri, 23 Oct 2026 13:00:00 GMT", now=NOW) == 0.0
    assert parse_retry_after(None, now=NOW) is None
    assert parse_retry_after("soon", now=NOW) is None
    assert parse_retry_after("-3", now=NOW) is None


async def test_sends_the_user_agent(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(URL).respond(200, json={"jobs": []})
    async with client(FakeSleep()) as http:
        response = await http.request("GET", URL)
    assert response.status_code == 200
    assert route.calls.last.request.headers["User-Agent"] == "jobpulse-test/0.1"


async def test_retries_5xx_with_backoff_then_succeeds(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200, json={})]
    )
    sleep = FakeSleep()
    async with client(sleep) as http:
        response = await http.request("GET", URL)
    assert response.status_code == 200
    assert route.call_count == 3
    assert len(sleep.calls) == 2
    assert 0.0 <= sleep.calls[0] <= 1.0
    assert 0.0 <= sleep.calls[1] <= 2.0


async def test_gives_up_after_four_attempts(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(URL).respond(503)
    sleep = FakeSleep()
    async with client(sleep) as http:
        response = await http.request("GET", URL)
    assert response.status_code == 503
    assert route.call_count == 4
    assert len(sleep.calls) == 3


async def test_does_not_retry_a_404(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(URL).respond(404)
    sleep = FakeSleep()
    async with client(sleep) as http:
        response = await http.request("GET", URL)
    assert response.status_code == 404
    assert route.call_count == 1
    assert sleep.calls == []


async def test_honors_retry_after_seconds(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)]
    )
    sleep = FakeSleep()
    async with client(sleep) as http:
        response = await http.request("GET", URL)
    assert response.status_code == 200
    assert sleep.calls == [7.0]


async def test_honors_retry_after_http_date(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(URL).mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "Fri, 23 Oct 2026 14:00:30 GMT"}),
            httpx.Response(200),
        ]
    )
    sleep = FakeSleep()
    async with client(sleep) as http:
        await http.request("GET", URL)
    assert sleep.calls == [30.0]


async def test_retry_after_beyond_the_cap_returns_the_429(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(URL).respond(429, headers={"Retry-After": "3600"})
    sleep = FakeSleep()
    async with client(sleep, retry=RetryPolicy(max_retry_after=120.0)) as http:
        response = await http.request("GET", URL)
    assert response.status_code == 429
    assert route.call_count == 1
    assert sleep.calls == []


async def test_timeouts_are_retried_then_raised(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    sleep = FakeSleep()
    async with client(sleep) as http:
        with pytest.raises(httpx.TimeoutException):
            await http.request("GET", URL)
    assert route.call_count == 4
    assert len(sleep.calls) == 3


async def test_connection_errors_are_retried(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(URL).mock(side_effect=[httpx.ConnectError("down"), httpx.Response(200)])
    async with client(FakeSleep()) as http:
        response = await http.request("GET", URL)
    assert response.status_code == 200


async def test_retry_statuses_can_be_narrowed(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(URL).respond(429, headers={"Retry-After": "1"})
    async with client(FakeSleep()) as http:
        response = await http.request("POST", URL, retry_statuses=frozenset({503}))
    assert response.status_code == 429
    assert route.call_count == 1


async def test_etag_cache_replays_the_body_on_304(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(URL).mock(
        side_effect=[
            httpx.Response(200, json={"jobs": [1]}, headers={"ETag": '"v1"'}),
            httpx.Response(304),
        ]
    )
    async with client(FakeSleep()) as http:
        first = await http.request("GET", URL, use_etag_cache=True)
        second = await http.request("GET", URL, use_etag_cache=True)
    assert "If-None-Match" not in route.calls[0].request.headers
    assert route.calls[1].request.headers["If-None-Match"] == '"v1"'
    assert first.json() == {"jobs": [1]}
    assert second.status_code == 200
    assert second.json() == {"jobs": [1]}
    assert second.headers[REPLAY_HEADER] == "1"


async def test_etag_cache_is_opt_in(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(URL).respond(200, json={}, headers={"ETag": '"v1"'})
    async with client(FakeSleep()) as http:
        await http.request("GET", URL)
        await http.request("GET", URL)
    assert "If-None-Match" not in route.calls[1].request.headers


async def test_per_host_concurrency_limit() -> None:
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200)

    async with HttpClient(
        user_agent="t",
        host_limits={"boards-api.greenhouse.io": 2},
        transport=httpx.MockTransport(handler),
    ) as http:
        await asyncio.gather(*(http.request("GET", f"{URL}?n={i}") for i in range(8)))
    assert peak == 2


PAGE = "https://careers.acme.example/jobs"


async def test_get_limited_cuts_the_body_off_at_the_cap(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(PAGE).respond(
        200, text="a" * 5000, headers={"Content-Type": "text/html"}
    )
    async with client(FakeSleep()) as http:
        response = await http.get_limited(PAGE, max_bytes=1000)
    assert response.status_code == 200
    assert response.content == b"a" * 1000
    assert response.headers["Content-Type"] == "text/html"
    assert route.calls.last.request.headers["User-Agent"] == "jobpulse-test/0.1"


async def test_get_limited_makes_one_attempt(respx_mock: respx.MockRouter) -> None:
    sleep = FakeSleep()
    route = respx_mock.get(PAGE).respond(503)
    async with client(sleep) as http:
        response = await http.get_limited(PAGE, max_bytes=1000)
    assert response.status_code == 503
    assert route.call_count == 1
    assert sleep.calls == []


async def test_get_limited_raises_transport_errors(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(PAGE).mock(side_effect=httpx.ConnectError("refused"))
    async with client(FakeSleep()) as http:
        with pytest.raises(httpx.ConnectError):
            await http.get_limited(PAGE, max_bytes=1000)
