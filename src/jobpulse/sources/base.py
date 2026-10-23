"""The SourceAdapter protocol and the shared base for HTTP adapters.

Adapters never raise for HTTP problems: a non-2xx response, a timeout or a parse error
becomes ``FetchResult(ok=False)``, never an empty success.
"""

from collections.abc import Callable, Collection, Mapping
from datetime import datetime
from typing import Any, Protocol

import httpx

from jobpulse.core.locations import DEFAULT_ALIASES
from jobpulse.core.models import (
    CompanyTarget,
    ErrorKind,
    FeedTarget,
    FetchResult,
    Posting,
    SearchTarget,
    Target,
)
from jobpulse.core.timeparse import parse_iso, utcnow
from jobpulse.httpclient import HttpClient

_BODY_SNIPPET = 200


class SourceAdapter(Protocol):
    name: str

    async def fetch(self, target: Target) -> FetchResult: ...

    async def enrich(self, posting: Posting) -> Posting: ...


class FetchError(Exception):
    """Raised inside an adapter; ``fetch`` turns it into a failed FetchResult."""

    def __init__(self, kind: ErrorKind, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.kind: ErrorKind = kind
        self.message = message
        self.status = status


def error_kind_for_status(status: int) -> ErrorKind:
    if status == 429:
        return "http_429"
    if 400 <= status < 500:
        return "http_4xx"
    if status >= 500:
        return "http_5xx"
    return "other"


def require_company(target: Target) -> CompanyTarget:
    if not isinstance(target, CompanyTarget):
        raise FetchError("other", f"{type(target).__name__} is not a company board target")
    return target


def require_search(target: Target) -> SearchTarget:
    if not isinstance(target, SearchTarget):
        raise FetchError("other", f"{type(target).__name__} is not a job-board search target")
    return target


def require_feed(target: Target) -> FeedTarget:
    if not isinstance(target, FeedTarget):
        raise FetchError("other", f"{type(target).__name__} is not a feed target")
    return target


def parse_timestamp(value: object) -> datetime | None:
    """An ISO timestamp from a payload, or ``None`` when it is missing or unreadable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_iso(value)
    except ValueError:
        return None


class HttpAdapter:
    """Base for adapters that read JSON over HTTP. Subclasses implement ``_fetch``."""

    name: str = ""

    def __init__(
        self,
        http: HttpClient,
        *,
        aliases: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.http = http
        self.aliases = aliases if aliases is not None else DEFAULT_ALIASES
        self.clock = clock

    async def fetch(self, target: Target) -> FetchResult:
        fetched_at = self.clock()
        try:
            return await self._fetch(target, fetched_at)
        except FetchError as exc:
            return FetchResult.failure(exc.kind, exc.message, fetched_at=fetched_at)

    async def _fetch(self, target: Target, fetched_at: datetime) -> FetchResult:
        raise NotImplementedError

    async def enrich(self, posting: Posting) -> Posting:
        return posting

    async def get_response(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        use_etag_cache: bool = False,
        accept: Collection[int] = (200,),
    ) -> httpx.Response:
        """GET, raising ``FetchError`` for a transport failure or a status not in ``accept``."""
        try:
            response = await self.http.request(
                "GET", url, headers=headers, use_etag_cache=use_etag_cache
            )
        except httpx.TimeoutException as exc:
            raise FetchError("timeout", f"timed out after retries ({type(exc).__name__})") from None
        except httpx.TransportError as exc:
            raise FetchError(
                "other", f"network error after retries ({type(exc).__name__})"
            ) from None
        if response.status_code not in accept:
            snippet = " ".join(response.text[:_BODY_SNIPPET].split())
            message = f"HTTP {response.status_code} from {response.url.host}"
            raise FetchError(
                error_kind_for_status(response.status_code),
                f"{message}: {snippet}" if snippet else message,
                status=response.status_code,
            )
        return response

    @staticmethod
    def decode_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError("parse", f"invalid JSON from {response.url.host}: {exc}") from None

    async def get_json(self, url: str, *, use_etag_cache: bool = False) -> Any:
        """GET and decode JSON, raising ``FetchError`` for anything but a readable 200."""
        return self.decode_json(await self.get_response(url, use_etag_cache=use_etag_cache))
