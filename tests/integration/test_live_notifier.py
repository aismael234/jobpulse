"""M6: the worker's notifier reads the profile's channels and the secrets at send time, so a
topic saved on Settings works from the next push, and with no channel a match waits (and is
retried while fresh) instead of being lost."""

from datetime import timedelta

import pytest
import respx
from pydantic import SecretStr

from jobpulse.config import NotifyConfig, Settings
from jobpulse.credentials import SecretStore
from jobpulse.notify.base import NotifierError
from jobpulse.notify.live import NO_CHANNEL, TEST_MESSAGE, LiveNotifier, channel_notifiers
from tests.factories import NOW
from tests.integration.conftest import Env, GreenhouseMock, gh_job

pytestmark = pytest.mark.respx(assert_all_called=False)

TOPIC = "jobpulse-Zq3vX9kR2mT7wL4pB8nC1dF6"
OLD = NOW - timedelta(days=3)
FRESH = NOW - timedelta(minutes=25)


def live(env: Env, settings: Settings | None = None) -> SecretStore:
    store = SecretStore(settings or Settings(_env_file=None))
    env.notifier = LiveNotifier(env.http, env.sessions, store)
    return store


async def test_with_no_channel_a_match_waits_and_goes_out_once_one_is_saved(
    env: Env, gh: GreenhouseMock, respx_mock: respx.MockRouter
) -> None:
    store = live(env)
    env.reconfigure(notify=NotifyConfig(channels=["ntfy"]))
    gh.respond(gh_job(101, published=OLD))
    await env.poll(sources=["greenhouse"])
    gh.respond(gh_job(101, published=OLD), gh_job(102, published=FRESH))

    report = await env.poll(sources=["greenhouse"])
    assert report.failed_pushes == 1
    assert env.posting("greenhouse", "102").notified_at is None

    with env.sessions() as session:
        store.save(session, "ntfy_topic", TOPIC, now=NOW)
        session.commit()
    ntfy = respx_mock.post(f"https://ntfy.sh/{TOPIC}").respond(200)
    report = await env.poll(sources=["greenhouse"])
    assert report.failed_pushes == 0
    assert ntfy.call_count == 1
    assert env.posting("greenhouse", "102").notified_at is not None


async def test_the_profile_names_the_channels(env: Env) -> None:
    store = live(env, Settings(_env_file=None, ntfy_topic=SecretStr(TOPIC)))
    assert isinstance(env.notifier, LiveNotifier)
    env.reconfigure(notify=NotifyConfig(channels=[]))
    assert env.notifier.channels() == []
    with pytest.raises(NotifierError, match="no notification channel"):
        await env.notifier.send(TEST_MESSAGE)
    env.reconfigure(notify=NotifyConfig(channels=["ntfy", "discord"]))
    assert [name for name, _ in env.notifier.channels()] == ["ntfy"]  # no webhook yet
    with env.sessions() as session:
        store.save(session, "discord_webhook_url", "https://discord.com/api/webhooks/1/x", now=NOW)
        session.commit()
    assert [name for name, _ in env.notifier.channels()] == ["ntfy", "discord"]


def test_channels_need_their_secret(env: Env) -> None:
    store = SecretStore(Settings(_env_file=None))
    with env.sessions() as session:
        secrets = store.resolve(session)
    notify = NotifyConfig(channels=["ntfy", "discord"])
    assert channel_notifiers(env.http, notify, secrets) == []
    assert "Settings" in NO_CHANNEL
