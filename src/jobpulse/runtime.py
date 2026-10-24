"""Wiring shared by the CLI and the worker: database, HTTP client, adapters and notifiers."""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable, Collection
from datetime import time, timedelta

import httpx
from sqlalchemy import inspect
from sqlalchemy.orm import Session, sessionmaker

from jobpulse.config import AppConfig, Settings, location_aliases
from jobpulse.db import migrate
from jobpulse.db.session import create_engine_for, display_url, make_session_factory
from jobpulse.digest import Digest, DigestResult
from jobpulse.discovery import Discoverer
from jobpulse.httpclient import HttpClient, RetryPolicy, build_user_agent
from jobpulse.notify.base import FanOutNotifier, Notification, Notifier
from jobpulse.notify.discord import DiscordNotifier
from jobpulse.notify.ntfy import NtfyNotifier
from jobpulse.poller import CycleReport, Poller
from jobpulse.scheduler import (
    WATCHDOG_FACTOR,
    Cadence,
    DailyTimes,
    JobFunc,
    Schedule,
    Worker,
    cadences_from_config,
)
from jobpulse.sources.ashby import AshbyAdapter
from jobpulse.sources.ats_detect import AtsDetector
from jobpulse.sources.base import SourceAdapter
from jobpulse.sources.greenhouse import GreenhouseAdapter
from jobpulse.sources.jobspy_boards import JobSpyAdapter, Pacing, Scrape
from jobpulse.sources.lever import LeverAdapter
from jobpulse.sources.simplify import SimplifyAdapter

log = logging.getLogger(__name__)

WATCHLIST_SOURCES: tuple[str, ...] = ("greenhouse", "lever", "ashby")
FEED_SOURCES: tuple[str, ...] = ("simplify",)
BOARD_SOURCES: tuple[str, ...] = ("indeed", "linkedin")
KNOWN_SOURCES: tuple[str, ...] = WATCHLIST_SOURCES + FEED_SOURCES + BOARD_SOURCES


class DatabaseNotReady(Exception):
    pass


class WorkerStalled(Exception):
    """The watchdog found a source that stopped finishing runs; exit so a supervisor
    (Docker's restart policy, systemd, launchd) starts the worker again."""


def open_database(url: str) -> sessionmaker[Session]:
    engine = create_engine_for(url)
    if not inspect(engine).has_table("postings"):
        engine.dispose()
        raise DatabaseNotReady(
            f"{display_url(url)} has no jobpulse tables yet; run `jobpulse db upgrade` first"
        )
    try:
        current, head = migrate.schema_revisions(engine)
    except FileNotFoundError:  # no alembic.ini next to this install: nothing to compare with
        current = head = None
    if current is not None and current != head:
        engine.dispose()
        raise DatabaseNotReady(
            f"{display_url(url)} is at migration {current}, but this version needs {head}; "
            "run `jobpulse db upgrade` first"
        )
    return make_session_factory(engine)


def build_http(
    config: AppConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> HttpClient:
    http = config.http
    return HttpClient(
        user_agent=build_user_agent(config.contact_email),
        timeout=http.timeout_seconds,
        retry=RetryPolicy(
            max_attempts=http.max_attempts,
            base_delay=http.backoff_base_seconds,
            max_delay=http.backoff_max_seconds,
            max_retry_after=http.max_retry_after_seconds,
        ),
        host_limits=http.host_limits,
        default_limit=http.default_host_limit,
        transport=transport,
    )


def build_adapters(
    http: HttpClient, config: AppConfig, *, scrape: Scrape | None = None
) -> dict[str, SourceAdapter]:
    """Every source's adapter. ``scrape`` replaces JobSpy's ``scrape_jobs`` (fixture mode)."""
    aliases = location_aliases(config)
    linkedin = config.linkedin
    boards = JobSpyAdapter(
        scrape=scrape,
        aliases=aliases,
        pacing=Pacing(
            delay_min=linkedin.delay_min_seconds,
            delay_max=linkedin.delay_max_seconds,
            cooldown=timedelta(minutes=linkedin.cooldown_minutes),
        ),
    )
    return {
        "greenhouse": GreenhouseAdapter(http, aliases=aliases),
        "lever": LeverAdapter(http, aliases=aliases),
        "ashby": AshbyAdapter(http, aliases=aliases),
        "simplify": SimplifyAdapter(http, aliases=aliases),
        "indeed": boards,
        "linkedin": boards,
    }


def build_discoverer(http: HttpClient, config: AppConfig) -> Discoverer | None:
    discovery = config.discovery
    if not discovery.enabled:
        return None
    return Discoverer(
        AtsDetector(http),
        max_validations=discovery.max_validations_per_cycle,
        max_page_fetches=discovery.max_page_fetches_per_cycle,
        require_match=discovery.require_match,
        max_board_jobs=discovery.max_board_jobs,
    )


def build_notifier(http: HttpClient, config: AppConfig, settings: Settings) -> Notifier | None:
    """The enabled channels whose secrets are set; ``None`` when there are none."""
    channels: list[Notifier] = []
    if "ntfy" in config.notify.channels and settings.ntfy_topic:
        channels.append(
            NtfyNotifier(
                http,
                base_url=config.notify.ntfy.base_url,
                topic=settings.ntfy_topic.get_secret_value(),
                token=settings.ntfy_token.get_secret_value() if settings.ntfy_token else None,
            )
        )
    if "discord" in config.notify.channels and settings.discord_webhook_url:
        channels.append(
            DiscordNotifier(
                http,
                webhook_url=settings.discord_webhook_url.get_secret_value(),
                username=config.notify.discord.username,
            )
        )
    if not channels:
        return None
    return channels[0] if len(channels) == 1 else FanOutNotifier(channels)


async def poll_once(
    *,
    config: AppConfig,
    settings: Settings,
    database_url: str,
    dry_run: bool,
    sources: Collection[str] | None = None,
    target: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    scrape: Scrape | None = None,
) -> CycleReport:
    """One cycle over ``sources`` (every source by default)."""
    sessions = open_database(database_url)
    async with build_http(config, transport=transport) as http:
        poller = Poller(
            config=config,
            sessions=sessions,
            adapters=build_adapters(http, config, scrape=scrape),
            notifier=None if dry_run else build_notifier(http, config, settings),
            discoverer=build_discoverer(http, config),
        )
        return await poller.run_cycle(sources=sources, target_key=target, dry_run=dry_run)


def render_report(report: CycleReport) -> list[str]:
    lines = ["dry run: nothing is sent and nothing is saved"] if report.dry_run else []
    for target in report.targets:
        head = f"{target.source}/{target.key}: "
        if target.skipped:
            lines.append(f"{head}skipped: {target.error}")
            continue
        if not target.ok:
            lines.append(f"{head}FAILED ({target.error_kind}) {target.error}")
            continue
        if target.not_modified:
            lines.append(f"{head}unchanged since the last fetch (304)")
            continue
        if target.is_baseline:
            postings = _count(target.baseline_stored, "posting")
            stored = (
                f"{postings} would be stored silently"
                if report.dry_run
                else f"stored {postings} silently"
            )
            if target.adopted:
                stored += f" ({_count(target.adopted, 'took over an earlier copy', 'took over earlier copies')})"
            lines.append(f"{head}baseline: {stored}")
            continue
        counts = [f"{target.fetched} fetched", f"{target.new} new"]
        for number, label in (
            (target.merged, "merged"),
            (target.adopted, "took over earlier copies"),
            (target.closed, "closed"),
            (target.reopened, "reopened"),
            (target.filtered, "filtered"),
            (target.stale, "stale"),
            (len(target.digest), "for the digest"),
        ):
            if number:
                counts.append(f"{number} {label}")
        lines.append(head + ", ".join(counts))
        lines.extend(f"  merged: {line}" for line in target.merges)
        lines.extend(f"  digest: {line}" for line in target.digest)
        lines.extend(f"  warning: {warning}" for warning in target.warnings)
    verb = "would notify" if report.dry_run else "notified"
    lines.extend(f"{verb}: {title}" for title in report.pushes)
    if not report.pushes:
        lines.append(f"{verb}: nothing")
    if report.held_back:
        lines.append(f"held back for the digest: {report.held_back} more instant matches")
    if report.failed_pushes:
        lines.append(f"failed pushes (retried next cycle): {report.failed_pushes}")
    health = "health (would send)" if report.dry_run else "health"
    lines.extend(f"{health}: {title}" for title in report.health)
    added = "would add" if report.dry_run else "added"
    lines.extend(f"discovery: {added} {board}" for board in report.discovered)
    lines.extend(f"discovery: skipped {board}" for board in report.discovery_skipped)
    if report.discovery_deferred:
        waiting = _count(report.discovery_deferred, "more candidate waits", "more candidates wait")
        lines.append(f"discovery: {waiting} for later cycles")
    if report.discovery_capped:
        boards = _count(report.discovery_capped, "matching board", "matching boards")
        lines.append(
            f"discovery: {boards} not added: discovery.max_auto_companies reached "
            "(raise it, or block companies you don't want)"
        )
    if report.pruned_runs:
        lines.append(
            f"housekeeping: deleted {_count(report.pruned_runs, 'run')} older than "
            f"{_count(report.retention_days or 0, 'day')} (retention.runs_days)"
        )
    if report.dry_run:
        lines.append("dry run: rolled back")
    return lines


async def run_worker(*, config: AppConfig, settings: Settings, database_url: str) -> None:
    """Poll each source on its own jittered schedule and send the digest at its times, until
    SIGINT or SIGTERM. Raises ``WorkerStalled`` when a source stops finishing runs."""
    sessions = open_database(database_url)
    async with build_http(config) as http:
        notifier = build_notifier(http, config, settings)
        poller = Poller(
            config=config,
            sessions=sessions,
            adapters=build_adapters(http, config),
            notifier=notifier,
            discoverer=build_discoverer(http, config),
        )
        digest = Digest(config=config, sessions=sessions, notifier=notifier)

        async def run(job: str, sources: tuple[str, ...]) -> None:
            report = await poller.run_cycle(sources=sources)
            for line in render_report(report):
                log.info("%s: %s", job, line)

        async def run_digest() -> None:
            for line in render_digest(await digest.run(), dry_run=False):
                log.info("digest: %s", line)

        jobs: dict[str, tuple[Schedule, JobFunc]] = {
            **scheduled_jobs(config, run),
            **digest_job(config, run_digest),
        }
        worker = Worker(jobs)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        worker.start()
        log.info(
            "worker started: %s", "; ".join(_describe(name, s) for name, (s, _) in jobs.items())
        )
        watchdog = asyncio.create_task(worker.watch())
        stopped = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({watchdog, stopped}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            worker.shutdown()
            for task in (watchdog, stopped):
                task.cancel()
            log.info("worker stopped")
        if watchdog.done() and not watchdog.cancelled():
            limit = worker.watchdog_limit or 0
            raise WorkerStalled(
                f"no run of {', '.join(watchdog.result())} finished in {_interval(limit)} "
                f"({WATCHDOG_FACTOR:.0f}x the longest interval); exiting so the worker is "
                "restarted"
            )


def scheduled_jobs(
    config: AppConfig, run: Callable[[str, tuple[str, ...]], Awaitable[None]]
) -> dict[str, tuple[Cadence, JobFunc]]:
    """One job per source group, each on its own cadence: the watchlist boards always, the
    Simplify feed when enabled, and the job-board searches when any is active."""
    cadences = cadences_from_config(config)
    groups: list[tuple[str, tuple[str, ...]]] = [("watchlist", WATCHLIST_SOURCES)]
    if config.simplify.enabled:
        groups.append(("simplify", FEED_SOURCES))
    if any(search.active for search in config.searches):
        groups.append(("job_boards", BOARD_SOURCES))

    def job(name: str, sources: tuple[str, ...]) -> JobFunc:
        async def poll() -> None:
            await run(name, sources)

        return poll

    return {name: (cadences[name], job(name, sources)) for name, sources in groups}


def digest_job(
    config: AppConfig, run: Callable[[], Awaitable[None]]
) -> dict[str, tuple[DailyTimes, JobFunc]]:
    """The digest at ``schedules.digest_times`` in ``timezone`` (none when the list is empty)."""
    times = sorted({time.fromisoformat(value) for value in config.schedules.digest_times})
    if not times:
        return {}

    async def job() -> None:
        await run()

    return {"digest": (DailyTimes(tuple(times), config.timezone), job)}


async def send_digest(
    *, config: AppConfig, settings: Settings, database_url: str, dry_run: bool
) -> DigestResult:
    """One digest now (``jobpulse digest``)."""
    sessions = open_database(database_url)
    async with build_http(config) as http:
        notifier = None if dry_run else build_notifier(http, config, settings)
        return await Digest(config=config, sessions=sessions, notifier=notifier).run(
            dry_run=dry_run
        )


def render_digest(result: DigestResult, *, dry_run: bool) -> list[str]:
    if result.notification is None:
        return ["digest: nothing to send"]
    if dry_run:
        message = result.notification
        return [
            "dry run: nothing is sent and nothing is marked",
            f"would send: {message.title}",
            *(f"  {line}" for line in message.body.splitlines()),
        ]
    if result.sent:
        return [f"digest sent: {_count(result.count, 'posting')}"]
    return [f"digest not sent ({_count(result.count, 'posting')} wait): {result.error}"]


def _count(number: int, singular: str, plural: str | None = None) -> str:
    return f"{number} {singular if number == 1 else plural or singular + 's'}"


def _interval(seconds: float) -> str:
    return f"{seconds:.0f} s" if seconds < 60 else f"{seconds / 60:.0f} min"


def _describe(name: str, schedule: Schedule) -> str:
    if isinstance(schedule, DailyTimes):
        times = ", ".join(at.strftime("%H:%M") for at in schedule.times)
        return f"{name} at {times} {schedule.timezone}"
    return f"{name} every {_interval(schedule.interval_seconds)} ± {schedule.jitter_seconds:.0f} s"


async def send_test_notification(*, config: AppConfig, settings: Settings) -> list[str]:
    async with build_http(config) as http:
        notifier = build_notifier(http, config, settings)
        if notifier is None:
            return []
        await notifier.send(
            Notification(
                title="jobpulse test · notifications work",
                body="This is a test push from `jobpulse notify test`.",
                priority=3,
                tags=("white_check_mark",),
            )
        )
        return [channel for channel in ("ntfy", "discord") if channel in config.notify.channels]
