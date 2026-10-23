"""Async HTTP client: per-host concurrency limits, retries with full-jitter backoff that
honors Retry-After, a descriptive User-Agent, and an opt-in ETag cache.

``request`` returns the final response whatever its status, so callers decide what an error
means. It raises only when every attempt failed at the transport level (timeouts,
connection errors).
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from jobpulse import __version__
from jobpulse.core.timeparse import utcnow

log = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
REPLAY_HEADER = "X-Jobpulse-Replayed"
_HEADERS_KEPT_ON_REPLAY = frozenset({"content-type", "etag", "last-modified"})
# get_limited hands back decoded, possibly cut-off bytes, so these no longer describe them.
_HEADERS_DROPPED_ON_TRUNCATION = frozenset(
    {"content-encoding", "content-length", "transfer-encoding"}
)

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 60.0
    max_retry_after: float = 120.0


DEFAULT_RETRY = RetryPolicy()


@dataclass(frozen=True, slots=True)
class _CachedBody:
    etag: str
    content: bytes
    headers: dict[str, str]


def _wire_headers(headers: Mapping[str, str | bytes] | None) -> dict[bytes, bytes]:
    """Header names and values as bytes. httpx would encode ``str`` values as ASCII, which
    rejects characters such as "·"; callers pass bytes when they need another encoding."""
    return {
        name.encode("ascii"): value if isinstance(value, bytes) else value.encode("utf-8")
        for name, value in (headers or {}).items()
    }


def build_user_agent(contact_email: str | None) -> str:
    detail = "+personal job alerts" + (f"; contact: {contact_email}" if contact_email else "")
    return f"jobpulse/{__version__} ({detail})"


def backoff_delay(retry_number: int, policy: RetryPolicy, rng: random.Random) -> float:
    """Full jitter: uniform(0, min(cap, base · 2^n)) for the n-th retry (n starts at 0)."""
    cap = min(policy.max_delay, policy.base_delay * 2**retry_number)
    return rng.uniform(0.0, cap)


def parse_retry_after(value: str | None, *, now: datetime) -> float | None:
    """Seconds to wait from a Retry-After header (seconds or an HTTP date), else ``None``."""
    if value is None:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - now).total_seconds())
    return seconds if seconds >= 0 else None


class HttpClient:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 20.0,
        retry: RetryPolicy = DEFAULT_RETRY,
        host_limits: Mapping[str, int] | None = None,
        default_limit: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": user_agent},
            transport=transport,
            follow_redirects=True,
        )
        self._retry = retry
        self._host_limits = {host.lower(): limit for host, limit in (host_limits or {}).items()}
        self._default_limit = default_limit
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._clock = clock
        self._etag_cache: dict[str, _CachedBody] = {}

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _semaphore(self, host: str) -> asyncio.Semaphore:
        semaphore = self._semaphores.get(host)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._host_limits.get(host, self._default_limit))
            self._semaphores[host] = semaphore
        return semaphore

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str | bytes] | None = None,
        json: Any = None,
        content: bytes | None = None,
        retry_statuses: frozenset[int] = RETRYABLE_STATUSES,
        use_etag_cache: bool = False,
    ) -> httpx.Response:
        host = (httpx.URL(url).host or "").lower()
        cacheable = use_etag_cache and method.upper() == "GET"
        attempts = self._retry.max_attempts
        for attempt in range(1, attempts + 1):
            send_headers = _wire_headers(headers)
            cached = self._etag_cache.get(url) if cacheable else None
            if cached is not None:
                send_headers[b"If-None-Match"] = cached.etag.encode("latin-1")
            try:
                # The host slot is held only while the request is in flight, never while sleeping.
                async with self._semaphore(host):
                    response = await self._client.request(
                        method, url, headers=send_headers, json=json, content=content
                    )
            except httpx.TransportError as exc:
                if attempt == attempts:
                    raise
                delay = backoff_delay(attempt - 1, self._retry, self._rng)
                log.info(
                    "%s %s failed (%s); retrying in %.1fs (attempt %d of %d)",
                    method, url, type(exc).__name__, delay, attempt + 1, attempts,
                )  # fmt: skip
                await self._sleep(delay)
                continue

            if response.status_code == 304 and cached is not None:
                return self._replay(cached, response.request)
            if response.status_code in retry_statuses and attempt < attempts:
                delay = backoff_delay(attempt - 1, self._retry, self._rng)
                retry_after = parse_retry_after(
                    response.headers.get("Retry-After"), now=self._clock()
                )
                if retry_after is not None:
                    if retry_after > self._retry.max_retry_after:
                        log.warning(
                            "%s %s: HTTP %d asks to wait %.0fs, more than the %.0fs cap; giving up",
                            method, url, response.status_code, retry_after,
                            self._retry.max_retry_after,
                        )  # fmt: skip
                        return response
                    delay = max(delay, retry_after)
                log.info(
                    "%s %s: HTTP %d; retrying in %.1fs (attempt %d of %d)",
                    method, url, response.status_code, delay, attempt + 1, attempts,
                )  # fmt: skip
                await self._sleep(delay)
                continue
            if cacheable and response.status_code == 200:
                etag = response.headers.get("ETag")
                if etag:
                    kept = {
                        name: value
                        for name, value in response.headers.items()
                        if name.lower() in _HEADERS_KEPT_ON_REPLAY
                    }
                    self._etag_cache[url] = _CachedBody(etag, response.content, kept)
            return response
        raise AssertionError("unreachable: the last attempt always returns or raises")

    async def get_limited(self, url: str, *, max_bytes: int) -> httpx.Response:
        """One GET (no retries) whose body is cut off after ``max_bytes``, for fetching pages
        whose size nobody promised. Raises ``httpx.TransportError`` like ``request``."""
        host = (httpx.URL(url).host or "").lower()
        async with self._semaphore(host), self._client.stream("GET", url) as response:
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():  # decoded, so the cap is on real bytes
                chunks.append(chunk)
                size += len(chunk)
                if size >= max_bytes:
                    break
        headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower() not in _HEADERS_DROPPED_ON_TRUNCATION
        }
        return httpx.Response(
            response.status_code,
            headers=headers,
            content=b"".join(chunks)[:max_bytes],
            request=response.request,
        )

    @staticmethod
    def _replay(cached: _CachedBody, request: httpx.Request) -> httpx.Response:
        """A 304 means the body is unchanged: hand back the cached one as a 200."""
        headers = {**cached.headers, REPLAY_HEADER: "1"}
        return httpx.Response(200, headers=headers, content=cached.content, request=request)
