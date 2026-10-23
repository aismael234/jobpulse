"""Item 13 (Discord): the JSON payload and honoring a 429's retry_after."""

import dataclasses
import json

import httpx
import pytest
import respx

from jobpulse.httpclient import HttpClient
from jobpulse.notify.base import Notification, NotifierError
from jobpulse.notify.discord import DiscordNotifier
from tests.factories import FakeSleep

WEBHOOK = "https://discord.com/api/webhooks/1234567890/secret-webhook-token"
MESSAGE = Notification(
    title="Stripe · Backend Engineer · SF / Remote · posted 25 min ago",
    body="Backend Engineer\nStripe · San Francisco, CA; Remote (US)",
    click_url="https://stripe.com/jobs/search?gh_jid=8172487",
    priority=4,
    tags=("briefcase",),
)


async def test_posts_one_embed(respx_mock: respx.MockRouter, http: HttpClient) -> None:
    route = respx_mock.post(WEBHOOK).respond(204)
    await DiscordNotifier(http, webhook_url=WEBHOOK).send(MESSAGE)

    payload = json.loads(route.calls.last.request.content)
    assert payload["username"] == "jobpulse"
    assert payload["allowed_mentions"] == {"parse": []}
    assert len(payload["embeds"]) == 1
    embed = payload["embeds"][0]
    assert embed["title"] == MESSAGE.title
    assert embed["url"] == MESSAGE.click_url
    assert embed["description"] == MESSAGE.body


async def test_429_waits_for_the_body_retry_after(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    route = respx_mock.post(WEBHOOK).mock(
        side_effect=[
            httpx.Response(
                429,
                json={
                    "message": "You are being rate limited.",
                    "retry_after": 1.5,
                    "global": False,
                },
                headers={"Retry-After": "2"},
            ),
            httpx.Response(204),
        ]
    )
    sleep = FakeSleep()
    await DiscordNotifier(http, webhook_url=WEBHOOK, sleep=sleep).send(MESSAGE)
    assert sleep.calls == [1.5]
    assert route.call_count == 2


async def test_429_falls_back_to_the_retry_after_header(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.post(WEBHOOK).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(204)]
    )
    sleep = FakeSleep()
    await DiscordNotifier(http, webhook_url=WEBHOOK, sleep=sleep).send(MESSAGE)
    assert sleep.calls == [2.0]


async def test_dead_webhook_raises_without_the_token(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.post(WEBHOOK).respond(404, json={"message": "Unknown Webhook", "code": 10015})
    with pytest.raises(NotifierError) as excinfo:
        await DiscordNotifier(http, webhook_url=WEBHOOK).send(MESSAGE)
    assert "secret-webhook-token" not in str(excinfo.value)


async def test_long_fields_are_truncated_to_discord_limits(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    route = respx_mock.post(WEBHOOK).respond(204)
    message = dataclasses.replace(MESSAGE, title="t" * 300, body="b" * 5000)
    await DiscordNotifier(http, webhook_url=WEBHOOK).send(message)
    embed = json.loads(route.calls.last.request.content)["embeds"][0]
    assert len(embed["title"]) == 256
    assert len(embed["description"]) == 4096
