"""The dashboard's state: everything the routes need, kept on ``app.state.web``."""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

from jobpulse.config import (
    AppConfig,
    InfraConfig,
    Profile,
    company_blocklist,
    compose,
    infra_for,
    short_labels,
)
from jobpulse.core.filters import Blocklist
from jobpulse.credentials import SecretStore
from jobpulse.db.profile import ProfileReader
from jobpulse.httpclient import HttpClient, build_user_agent
from jobpulse.seed import REMOTE_TARGET
from jobpulse.seed import SOURCE as SEED_SOURCE
from jobpulse.sources.ats_detect import AtsDetector


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The profile as the dashboard sees it, composed with how the worker runs for it."""

    config: AppConfig
    revision: int  # 0: no profile stored (the defaults show)
    onboarded: bool
    problem: str | None  # the stored profile is invalid (the last good one shows)
    blocklist: Blocklist  # the profile's; routes add the dashboard's blocks per request
    short_labels: dict[str, str]
    zone: ZoneInfo
    feeds: list[tuple[str, str]]  # the feeds the Health page lists: Simplify, the seed sync


@dataclass
class WebState:
    """Everything the routes need, kept on ``app.state.web``. ``current()`` reads the
    profile's revision per call and composes again only when it moved, so a change saved on
    Settings (or by the CLI) shows without a restart."""

    infra: InfraConfig  # the base: the profile's Sources & schedule settings apply to it
    sessions: sessionmaker[Session]
    templates: Jinja2Templates
    clock: Callable[[], datetime]
    store: SecretStore
    detector: AtsDetector | None = None
    http: HttpClient | None = None
    tasks: set["asyncio.Task[Any]"] = field(default_factory=set)  # background work (the seed)
    _profiles: ProfileReader = field(default_factory=ProfileReader)
    _snapshot: Snapshot | None = None

    def current(self) -> Snapshot:
        with self.sessions() as session:
            stored = self._profiles.read(session)
        snapshot = self._snapshot
        if (
            snapshot is not None
            and snapshot.revision == stored.revision
            and snapshot.problem == stored.problem
        ):
            return snapshot
        profile = stored.profile or Profile()
        config = compose(infra_for(profile, base=self.infra), profile)
        snapshot = Snapshot(
            config=config,
            revision=stored.revision,
            onboarded=stored.onboarded,
            problem=stored.problem,
            blocklist=company_blocklist(config),
            short_labels=short_labels(config),
            zone=ZoneInfo(config.timezone),
            feeds=([("simplify", config.simplify.name)] if config.simplify.enabled else [])
            + ([(SEED_SOURCE, REMOTE_TARGET)] if config.seed.remote else []),
        )
        self._snapshot = snapshot
        if self.http is not None:
            self.http.set_user_agent(build_user_agent(config.contact_email))
        return snapshot

    def spawn(self, work: Coroutine[Any, Any, Any]) -> "asyncio.Task[Any]":
        """Run ``work`` in the background, keeping a reference until it ends."""
        task = asyncio.get_running_loop().create_task(work)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task
