"""Discord webhook notifier: one embed per message, mentions disabled. Success is 204.

On a 429 it waits for the body's ``retry_after`` (float seconds), else the Retry-After
header, then retries. The HTTP client's own 429 handling is turned off for this reason.
"""

import asyncio

import httpx

from jobpulse.core.timeparse import utcnow
from jobpulse.httpclient import HttpClient, Sleep, parse_retry_after
from jobpulse.notify.base import Notification, NotifierError

TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
_SERVER_ERRORS = frozenset({500, 502, 503, 504})
_PRIORITY_COLORS = {1: 0x95A5A6, 2: 0x95A5A6, 3: 0x3498DB, 4: 0x2ECC71, 5: 0xE74C3C}


def _retry_after(response: httpx.Response) -> float:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        value = body.get("retry_after")
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    header = parse_retry_after(response.headers.get("Retry-After"), now=utcnow())
    return header if header is not None else 1.0


class DiscordNotifier:
    name = "discord"

    def __init__(
        self,
        http: HttpClient,
        *,
        webhook_url: str,
        username: str = "jobpulse",
        sleep: Sleep = asyncio.sleep,
        max_attempts: int = 4,
        max_wait: float = 60.0,
    ) -> None:
        self._http = http
        self._url = webhook_url
        self._username = username
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._max_wait = max_wait

    async def send(self, message: Notification) -> None:
        embed: dict[str, object] = {
            "title": message.title[:TITLE_LIMIT],
            "description": message.body[:DESCRIPTION_LIMIT],
            "color": _PRIORITY_COLORS[message.priority],
        }
        if message.click_url:
            embed["url"] = message.click_url
        payload = {
            "username": self._username,
            "allowed_mentions": {"parse": []},
            "embeds": [embed],
        }
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._http.request(
                    "POST", self._url, json=payload, retry_statuses=_SERVER_ERRORS
                )
            except httpx.TransportError as exc:
                raise NotifierError(f"Discord unreachable ({type(exc).__name__})") from None
            if response.status_code in (200, 204):
                return
            if response.status_code == 429 and attempt < self._max_attempts:
                wait = _retry_after(response)
                if wait > self._max_wait:
                    raise NotifierError(f"Discord rate limit asks for {wait:.0f}s; giving up")
                await self._sleep(wait)
                continue
            if response.status_code == 404:
                raise NotifierError("Discord webhook not found (was it deleted?)")
            raise NotifierError(f"Discord answered HTTP {response.status_code}")
        raise NotifierError("Discord kept rate limiting; giving up")
