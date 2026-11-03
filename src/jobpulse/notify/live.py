"""Notifications through the channels the profile names, with secrets resolved at send time
(M6): a topic or webhook added on Settings works from the next message, without a restart."""

from sqlalchemy.orm import Session, sessionmaker

from jobpulse.config import NotifyConfig, Profile
from jobpulse.credentials import Secrets, SecretStore
from jobpulse.db.profile import ProfileReader
from jobpulse.httpclient import HttpClient
from jobpulse.notify.base import FanOutNotifier, Notification, Notifier, NotifierError
from jobpulse.notify.discord import DiscordNotifier
from jobpulse.notify.ntfy import NtfyNotifier

NO_CHANNEL = (
    "no notification channel is set up: add an ntfy topic or a Discord webhook on Settings "
    "(notify.channels names the channels to use)"
)


def channel_notifiers(
    http: HttpClient, notify: NotifyConfig, secrets: Secrets
) -> list[tuple[str, Notifier]]:
    """The channels ``notify.channels`` names whose secrets are set, by name."""
    channels: list[tuple[str, Notifier]] = []
    topic = secrets.get("ntfy_topic")
    if "ntfy" in notify.channels and topic:
        notifier = NtfyNotifier(
            http, base_url=notify.ntfy.base_url, topic=topic, token=secrets.get("ntfy_token")
        )
        channels.append(("ntfy", notifier))
    webhook = secrets.get("discord_webhook_url")
    if "discord" in notify.channels and webhook:
        discord = DiscordNotifier(http, webhook_url=webhook, username=notify.discord.username)
        channels.append(("discord", discord))
    return channels


class LiveNotifier:
    """Reads the profile's channels and the secrets on every send. With no channel set up,
    ``send`` raises ``NotifierError``, so the poller keeps the matches and retries them while
    they're fresh."""

    name = "live"

    def __init__(
        self, http: HttpClient, sessions: sessionmaker[Session], store: SecretStore
    ) -> None:
        self._http = http
        self._sessions = sessions
        self._store = store
        self._profiles = ProfileReader()

    def channels(self) -> list[tuple[str, Notifier]]:
        with self._sessions() as session:
            profile = self._profiles.read(session).profile or Profile()
            secrets = self._store.resolve(session)
        return channel_notifiers(self._http, profile.notify, secrets)

    async def send(self, message: Notification) -> None:
        channels = [notifier for _, notifier in self.channels()]
        if not channels:
            raise NotifierError(NO_CHANNEL)
        notifier = channels[0] if len(channels) == 1 else FanOutNotifier(channels)
        await notifier.send(message)


TEST_MESSAGE = Notification(
    title="jobpulse test · notifications work",
    body="This is a test push from jobpulse.",
    priority=3,
    tags=("white_check_mark",),
)


async def send_test(notifier: LiveNotifier) -> list[str]:
    """One test push through every channel that's set up; the names of those channels."""
    names = [name for name, _ in notifier.channels()]
    if names:
        await notifier.send(TEST_MESSAGE)
    return names
