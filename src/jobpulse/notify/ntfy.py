"""ntfy notifier: POST ``{base_url}/{topic}`` with a plain-text body and Title, Click,
Priority and Tags headers. If any header value isn't Latin-1, the message goes to the server
root as JSON instead.

httpx encodes ``str`` header values as ASCII and ntfy decodes header bytes as UTF-8, so the
values are sent as UTF-8 bytes; that keeps Latin-1 characters such as "·" intact.
"""

import httpx

from jobpulse.httpclient import HttpClient
from jobpulse.notify.base import Notification, NotifierError

MAX_BODY_BYTES = 4000  # ntfy turns larger bodies (over 4,096 bytes) into attachments
MAX_TITLE_CHARS = 250


def _fits_latin1(value: str) -> bool:
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


def _truncate_utf8(text: str, limit: int) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


class NtfyNotifier:
    name = "ntfy"

    def __init__(
        self, http: HttpClient, *, base_url: str, topic: str, token: str | None = None
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._topic = topic
        self._token = token

    async def send(self, message: Notification) -> None:
        title = message.title[:MAX_TITLE_CHARS]
        body = _truncate_utf8(message.body, MAX_BODY_BYTES)
        auth: dict[str, str | bytes] = (
            {"Authorization": f"Bearer {self._token}"} if self._token else {}
        )
        fields = {"Title": title, "Priority": str(message.priority)}
        if message.click_url:
            fields["Click"] = message.click_url
        if message.tags:
            fields["Tags"] = ",".join(message.tags)
        try:
            if all(_fits_latin1(value) for value in fields.values()):
                headers = {
                    **auth,
                    **{name: value.encode("utf-8") for name, value in fields.items()},
                }
                response = await self._http.request(
                    "POST",
                    f"{self._base_url}/{self._topic}",
                    headers=headers,
                    content=body.encode(),
                )
            else:
                payload: dict[str, object] = {
                    "topic": self._topic,
                    "title": title,
                    "message": body,
                    "priority": message.priority,
                    "tags": list(message.tags),
                }
                if message.click_url:
                    payload["click"] = message.click_url
                response = await self._http.request(
                    "POST", f"{self._base_url}/", headers=auth, json=payload
                )
        except httpx.TransportError as exc:
            # The exception text can contain the URL, and with it the topic.
            raise NotifierError(f"ntfy server unreachable ({type(exc).__name__})") from None
        if not response.is_success:
            raise NotifierError(f"ntfy answered HTTP {response.status_code}")
