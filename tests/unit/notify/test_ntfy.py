"""Item 13 (ntfy): URL, headers, body, auth and the JSON fallback."""

import dataclasses
import json

import httpx
import pytest
import respx

from jobpulse.httpclient import HttpClient
from jobpulse.notify.base import Notification, NotifierError
from jobpulse.notify.ntfy import NtfyNotifier

BASE = "https://ntfy.example"
TOPIC = "jobpulse-Zq3vX9kR2mT7wL4pB8nC1dF6"
MESSAGE = Notification(
    title="Stripe · Backend Engineer · SF / Remote · posted 25 min ago",
    body="Backend Engineer\nStripe · San Francisco, CA; Remote (US)",
    click_url="https://stripe.com/jobs/search?gh_jid=8172487",
    priority=4,
    tags=("briefcase", "greenhouse"),
)
OK = {"id": "Xa1", "time": 1792764000, "event": "message", "topic": TOPIC}


async def test_publishes_with_headers(respx_mock: respx.MockRouter, http: HttpClient) -> None:
    route = respx_mock.post(f"{BASE}/{TOPIC}").respond(200, json=OK)
    await NtfyNotifier(http, base_url=BASE, topic=TOPIC).send(MESSAGE)

    request = route.calls.last.request
    assert request.headers["Title"] == MESSAGE.title
    assert request.headers["Click"] == MESSAGE.click_url
    assert request.headers["Priority"] == "4"
    assert request.headers["Tags"] == "briefcase,greenhouse"
    assert request.content.decode("utf-8") == MESSAGE.body
    assert "Authorization" not in request.headers


async def test_bearer_token_for_a_self_hosted_server(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    route = respx_mock.post(f"{BASE}/{TOPIC}").respond(200, json=OK)
    await NtfyNotifier(http, base_url=BASE, topic=TOPIC, token="tk_example").send(MESSAGE)
    assert route.calls.last.request.headers["Authorization"] == "Bearer tk_example"


async def test_non_latin1_title_is_published_as_json(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    message = dataclasses.replace(MESSAGE, title="株式会社サンプル · バックエンドエンジニア")
    root = respx_mock.post(f"{BASE}/").respond(200, json=OK)
    await NtfyNotifier(http, base_url=BASE, topic=TOPIC).send(message)

    request = root.calls.last.request
    assert json.loads(request.content) == {
        "topic": TOPIC,
        "title": message.title,
        "message": message.body,
        "click": message.click_url,
        "priority": 4,
        "tags": ["briefcase", "greenhouse"],
    }
    assert "Title" not in request.headers


async def test_long_body_stays_under_the_attachment_limit(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    route = respx_mock.post(f"{BASE}/{TOPIC}").respond(200, json=OK)
    long_message = dataclasses.replace(MESSAGE, body="é" * 5000)
    await NtfyNotifier(http, base_url=BASE, topic=TOPIC).send(long_message)
    assert len(route.calls.last.request.content) <= 4000


async def test_rejected_publish_raises_without_the_topic(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.post(f"{BASE}/{TOPIC}").respond(403, json={"error": "forbidden"})
    with pytest.raises(NotifierError) as excinfo:
        await NtfyNotifier(http, base_url=BASE, topic=TOPIC).send(MESSAGE)
    assert "403" in str(excinfo.value)
    assert TOPIC not in str(excinfo.value)


async def test_unreachable_server_raises_without_the_topic(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.post(f"{BASE}/{TOPIC}").mock(side_effect=httpx.ConnectError(f"cannot reach {TOPIC}"))
    with pytest.raises(NotifierError) as excinfo:
        await NtfyNotifier(http, base_url=BASE, topic=TOPIC).send(MESSAGE)
    assert TOPIC not in str(excinfo.value)
