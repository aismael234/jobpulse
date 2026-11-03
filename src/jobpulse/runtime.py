"""Wiring shared by the CLI and the worker: database, HTTP client, adapters and notifiers."""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable, Collection
from datetime import timedelta
from pathlib import Path

import httpx
from sqlalchemy import inspect
from sqlalchemy.orm import Session, sessionmaker

from jobpulse.config import AppConfig, InfraConfig, infra_for, location_aliases
from jobpulse.core.locations import merge_aliases
from jobpulse.core.timeparse import utcnow
from jobpulse.credentials import SecretStore
from jobpulse.db import lease, migrate
from jobpulse.db import profile as profile_db
from jobpulse.db.session import (
    create_engine_for,
    display_url,
    make_session_factory,
    session_scope,
)
from jobpulse.discovery import Discoverer
from jobpulse.embeddings.base import Embedder, EmbedderUnavailable, LazyEmbedder
from jobpulse.embeddings.fastembed import DEFAULT_MODEL, FastEmbedEmbedder, model_id
from jobpulse.httpclient import HttpClient, RetryPolicy, build_user_agent
from jobpulse.llm.source import LlmSource
from jobpulse.notify.live import LiveNotifier, send_test
from jobpulse.poller import CycleReport, Poller
from jobpulse.scheduler import (
    WATCHDOG_FACTOR,
    Cadence,
    JobFunc,
    Worker,
    cadences_from_config,
)
from jobpulse.seed import SeedSync
from jobpulse.sources.ashby import AshbyAdapter
from jobpulse.sources.ats_detect import AtsDetector
from jobpulse.sources.base import SourceAdapter
from jobpulse.sources.greenhouse import GreenhouseAdapter
from jobpulse.sources.jobspy_boards import JobSpyAdapter, Scrape
from jobpulse.sources.lever import LeverAdapter
from jobpulse.sources.linkedin_gate import LinkedInGate, Pacing
from jobpulse.sources.linkedin_guest import LinkedInJobPages, LinkedInSource
from jobpulse.sources.maintenance import MaintenanceGate, Sleep
from jobpulse.sources.simplify import SimplifyAdapter
from jobpulse.sources.workday import WorkdayAdapter

log = logging.getLogger(__name__)

WATCHLIST_SOURCES: tuple[str, ...] = ("greenhouse", "lever", "ashby", "workday")
FEED_SOURCES: tuple[str, ...] = ("simplify",)
BOARD_SOURCES: tuple[str, ...] = ("indeed", "linkedin")
KNOWN_SOURCES: tuple[str, ...] = WATCHLIST_SOURCES + FEED_SOURCES + BOARD_SOURCES
IDLE_RECHECK_SECONDS = 60.0  # until a profile is onboarded, each job checks again this soon
SETTINGS_CHECK_SECONDS = 30.0  # how soon a Sources & schedule change reaches the worker


class DatabaseNotReady(Exception):
    pass


class LeaseLost(Exception):
    """Another worker took the lease while this one ran."""


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
    current, head = migrate.schema_revisions(engine)
    if current is not None and current != head:
        engine.dispose()
        raise DatabaseNotReady(
            f"{display_url(url)} is at migration {current}, but this version needs {head}; "
            "run `jobpulse db upgrade` first"
        )
    return make_session_factory(engine)


def stored_infra(
    sessions: sessionmaker[Session],
    *,
    data_dir: Path | None = None,
    base: InfraConfig | None = None,
) -> InfraConfig:
    """How the worker runs for the stored profile (its Sources & schedule settings); the
    defaults before onboarding."""
    with sessions() as session:
        profile = profile_db.get(session).profile
    return infra_for(profile, data_dir=data_dir, base=base)


def build_http(
    config: InfraConfig,
    *,
    contact_email: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HttpClient:
    """The shared client. The contact email is the profile's, so ``follow_profile`` updates
    the User-Agent when it changes."""
    http = config.http
    return HttpClient(
        user_agent=build_user_agent(contact_email),
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
    http: HttpClient,
    config: InfraConfig,
    *,
    aliases: dict[str, str] | None = None,
    scrape: Scrape | None = None,
    sleep: Sleep = asyncio.sleep,
) -> dict[str, SourceAdapter]:
    """Every source's adapter. ``scrape`` replaces JobSpy's ``scrape_jobs`` (fixture mode).
    LinkedIn's searches and job pages share one gate, so all LinkedIn requests are paced and
    cooled down together. Each HTTP source has its own maintenance gate, which ``sleep``
    paces. ``aliases`` is shared with every adapter: ``follow_profile`` updates it in place
    when the profile's location aliases change."""
    aliases = aliases if aliases is not None else merge_aliases({})

    def maintenance(name: str) -> MaintenanceGate:
        settings = config.maintenance
        return MaintenanceGate(
            name,
            retries=settings.retries,
            retry_delay=timedelta(seconds=settings.retry_delay_seconds),
            cooldown=timedelta(minutes=settings.cooldown_minutes),
            sleep=sleep,
        )

    linkedin = config.linkedin
    gate = LinkedInGate(
        Pacing(
            delay_min=linkedin.delay_min_seconds,
            delay_max=linkedin.delay_max_seconds,
            cooldown=timedelta(minutes=linkedin.cooldown_minutes),
        )
    )
    boards = JobSpyAdapter(scrape=scrape, aliases=aliases, gate=gate)
    pages = LinkedInJobPages(http, gate, max_per_hour=linkedin.max_job_pages_per_hour)
    return {
        "greenhouse": GreenhouseAdapter(
            http, aliases=aliases, maintenance=maintenance("greenhouse")
        ),
        "lever": LeverAdapter(http, aliases=aliases, maintenance=maintenance("lever")),
        "ashby": AshbyAdapter(http, aliases=aliases, maintenance=maintenance("ashby")),
        "workday": WorkdayAdapter(
            http,
            aliases=aliases,
            max_pages=config.workday.max_pages,
            maintenance=maintenance("workday"),
        ),
        "simplify": SimplifyAdapter(http, aliases=aliases, maintenance=maintenance("simplify")),
        "indeed": boards,
        "linkedin": LinkedInSource(boards, pages) if linkedin.job_pages else boards,
    }


def build_discoverer(http: HttpClient, config: InfraConfig) -> Discoverer | None:
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


def build_seed_sync(http: HttpClient, config: InfraConfig) -> SeedSync | None:
    """The public seed's daily sync, while ``seed.remote`` is on."""
    if not config.seed.remote:
        return None
    return SeedSync(http, AtsDetector(http), config.seed)


def build_embedder(config: InfraConfig) -> Embedder | None:
    """The embedding model behind resume similarity, loaded only when first used (a resume is
    stored). ``None`` when ``embeddings.enabled`` is false or fastembed isn't installed."""
    if not config.embeddings.enabled:
        return None
    cache_dir = config.embeddings.cache_dir
    try:
        model = model_id(DEFAULT_MODEL)
    except EmbedderUnavailable as exc:
        log.warning("resume similarity is off: %s", exc)
        return None
    return LazyEmbedder(model, lambda: FastEmbedEmbedder(DEFAULT_MODEL, cache_dir))


def follow_profile(http: HttpClient, aliases: dict[str, str]) -> Callable[[AppConfig], None]:
    """What a new profile revision changes outside the poller: the User-Agent's contact
    email, and the location aliases the adapters share."""

    def apply(config: AppConfig) -> None:
        http.set_user_agent(build_user_agent(config.contact_email))
        fresh = location_aliases(config)
        if fresh != aliases:
            aliases.clear()
            aliases.update(fresh)

    return apply


def build_poller(
    http: HttpClient,
    infra: InfraConfig,
    sessions: sessionmaker[Session],
    store: SecretStore,
    *,
    llm: LlmSource,
    dry_run: bool = False,
    scrape: Scrape | None = None,
) -> Poller:
    aliases = merge_aliases({})
    return Poller(
        infra=infra,
        sessions=sessions,
        adapters=build_adapters(http, infra, aliases=aliases, scrape=scrape),
        notifier=None if dry_run else LiveNotifier(http, sessions, store),
        discoverer=build_discoverer(http, infra),
        llm=llm,
        embedder=build_embedder(infra),
        on_profile=follow_profile(http, aliases),
        seed_sync=build_seed_sync(http, infra),
    )


async def poll_once(
    *,
    infra: InfraConfig,
    store: SecretStore,
    database_url: str,
    dry_run: bool,
    sources: Collection[str] | None = None,
    target: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    scrape: Scrape | None = None,
) -> CycleReport:
    """One cycle over ``sources`` (every source by default)."""
    sessions = open_database(database_url)
    llm = LlmSource(store, dry_run=dry_run)
    try:
        async with build_http(infra, transport=transport) as http:
            poller = build_poller(
                http, infra, sessions, store, llm=llm, dry_run=dry_run, scrape=scrape
            )
            return await poller.run_cycle(sources=sources, target_key=target, dry_run=dry_run)
    finally:
        await llm.aclose()


def render_report(report: CycleReport) -> list[str]:
    lines = ["dry run: nothing is sent and nothing is saved"] if report.dry_run else []
    if report.profile_problem:
        lines.append(f"profile: {report.profile_problem}")
    if report.idle:
        lines.append(f"idle: {report.idle}")
        return lines
    if report.rescored:
        matches = _count(report.rescored, "open match", "open matches")
        lines.append(f"profile: re-scored {matches} (filters or scoring changed)")
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
            (target.waiting, "waiting for their job page"),
        ):
            if number:
                counts.append(f"{number} {label}")
        lines.append(head + ", ".join(counts))
        lines.extend(f"  merged: {line}" for line in target.merges)
        lines.extend(f"  warning: {warning}" for warning in target.warnings)
    verb = "would notify" if report.dry_run else "notified"
    if report.push_summary:
        lines.append(f"{'push (would send)' if report.dry_run else 'push'}: {report.push_summary}")
    lines.extend(f"{verb}: {title}" for title in report.pushes)
    if not report.pushes:
        lines.append(f"{verb}: nothing")
    if report.failed_pushes:
        matches = _count(report.failed_pushes, "match", "matches")
        lines.append(f"push failed (retried next cycle): {matches}")
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
    lines.extend(_seed_lines(report))
    lines.extend(_llm_lines(report))
    lines.extend(_resume_lines(report))
    if report.pruned_runs:
        lines.append(
            f"housekeeping: deleted {_count(report.pruned_runs, 'run')} older than "
            f"{_count(report.retention_days or 0, 'day')} (retention.runs_days)"
        )
    if report.dry_run:
        lines.append("dry run: rolled back")
    return lines


def _seed_lines(report: CycleReport) -> list[str]:
    seed = report.seed
    if seed is None:
        return []
    if seed.problem:
        return [f"seed: sync FAILED: {seed.problem}"]
    if seed.not_modified:
        return ["seed: unchanged since the last sync (304)"]
    lines = [
        f"seed: {_count(seed.boards, 'board')} in the public seed, "
        f"added {len(seed.added)}, paused {len(seed.retired)}"
    ]
    lines.extend(f"seed: added {label}" for label in seed.added)
    lines.extend(f"seed: paused {label} (removed from the seed)" for label in seed.retired)
    lines.extend(f"seed: skipped {line}" for line in seed.skipped)
    if seed.capped:
        lines.append(
            f"seed: {_count(seed.capped, 'more new board waits', 'more new boards wait')} for the "
            "next sync (seed.max_new_boards)"
        )
    return lines


def _resume_lines(report: CycleReport) -> list[str]:
    lines: list[str] = []
    if report.resume_rescored:
        lines.append(
            f"resume: re-scored {_count(report.resume_rescored, 'open match', 'open matches')}"
        )
    if report.resume_matched:
        cached = report.resume_matched - report.resume_embedded
        lines.append(
            f"resume: {_count(report.resume_matched, 'match', 'matches')} scored "
            f"({report.resume_embedded} embedded, {max(0, cached)} from the cache)"
        )
    if report.resume_failed:
        lines.append(f"resume: {report.resume_failed}; stack keywords used instead")
    return lines


def _llm_lines(report: CycleReport) -> list[str]:
    lines: list[str] = []
    if report.llm_asked or report.llm_failed:
        parts = [f"{report.llm_asked} answered"]
        if report.llm_cached:
            parts.append(f"{report.llm_cached} from the cache")
        if report.llm_failed:
            parts.append(f"{report.llm_failed} failed")
        lines.append("llm: " + ", ".join(parts))
    elif report.llm_cached:
        lines.append(f"llm: {_count(report.llm_cached, 'answer')} from the cache")
    if report.llm_over_limit:
        left = _count(report.llm_over_limit, "posting")
        lines.append(f"llm: {left} left to the rules: llm.max_calls_per_day reached")
    if report.llm_would_ask:
        asked = _count(report.llm_would_ask, "posting")
        lines.append(f"llm: would ask about {asked} (a dry run never calls it)")
    if report.llm_stopped:
        lines.append(f"llm: stopped asking: {report.llm_stopped}")
    return lines


async def run_worker(
    *,
    store: SecretStore,
    database_url: str,
    data_dir: Path | None = None,
    base: InfraConfig | None = None,
    stop: asyncio.Event | None = None,
    holder: lease.Holder | None = None,
    settings_check_seconds: float = SETTINGS_CHECK_SECONDS,
) -> None:
    """Poll each source on its own jittered schedule until ``stop`` is set (by default, until
    SIGINT or SIGTERM). Raises ``LeaseHeld`` when another worker polls this database, and
    ``WorkerStalled`` when a source stops finishing runs. Cycles wait (and say so) until a
    profile is onboarded, checking again every minute; secrets are read at send time, so
    none is needed to start. When Settings → Sources & schedule changes how it should run,
    the schedule is rebuilt within ``settings_check_seconds``; a cycle in flight is cut short
    and runs again on the new schedule."""
    sessions = open_database(database_url)
    me = holder or lease.Holder.here()
    with session_scope(sessions) as session:
        lease.acquire(session, me, now=utcnow())
    llm = LlmSource(store)
    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
    stopped = asyncio.create_task(stop.wait())
    renewing = asyncio.create_task(_keep_lease(sessions, me))
    try:
        while True:
            infra = stored_infra(sessions, data_dir=data_dir, base=base)
            changed = await _run_schedule(
                infra,
                sessions,
                store,
                llm,
                stopped=stopped,
                renewing=renewing,
                settings_changed=_settings_changed(
                    sessions, infra, data_dir=data_dir, base=base, every=settings_check_seconds
                ),
            )
            if not changed:
                break
            log.info("Sources & schedule changed on Settings: rebuilding the schedule")
        if renewing.done() and not renewing.cancelled():
            renewing.result()  # raises LeaseLost
    finally:
        for task in (stopped, renewing):
            task.cancel()
        await llm.aclose()
        with session_scope(sessions) as session:
            lease.release(session, me)


async def _run_schedule(
    infra: InfraConfig,
    sessions: sessionmaker[Session],
    store: SecretStore,
    llm: LlmSource,
    *,
    stopped: "asyncio.Task[object]",
    renewing: "asyncio.Task[None]",
    settings_changed: Awaitable[None],
) -> bool:
    """Run the scheduled jobs for ``infra`` until stopped, the lease is lost, a source
    stalls (``WorkerStalled``) or the settings change; True for the last."""
    changed = asyncio.ensure_future(settings_changed)
    async with build_http(infra) as http:
        poller = build_poller(http, infra, sessions, store, llm=llm)

        async def run(job: str, sources: tuple[str, ...]) -> float | None:
            return await run_scheduled_cycle(poller, job, sources)

        jobs = scheduled_jobs(infra, run)
        worker = Worker(jobs)
        worker.start()
        log.info(
            "worker started: %s", "; ".join(_describe(name, s) for name, (s, _) in jobs.items())
        )
        watchdog = asyncio.create_task(worker.watch())
        try:
            await asyncio.wait(
                {watchdog, stopped, renewing, changed}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            worker.shutdown()
            for task in (watchdog, changed):
                task.cancel()
            log.info("worker stopped")
    if watchdog.done() and not watchdog.cancelled():
        limit = worker.watchdog_limit or 0
        raise WorkerStalled(
            f"no run of {', '.join(watchdog.result())} finished in {_interval(limit)} "
            f"({WATCHDOG_FACTOR:.0f}x the longest interval); exiting so the worker is restarted"
        )
    return changed.done() and not changed.cancelled() and not stopped.done()


async def _settings_changed(
    sessions: sessionmaker[Session],
    running: InfraConfig,
    *,
    data_dir: Path | None,
    base: InfraConfig | None,
    every: float,
    sleep: Sleep = asyncio.sleep,
) -> None:
    """Return once the stored profile asks for a different ``InfraConfig`` than ``running``."""
    while True:
        await sleep(every)
        try:
            wanted = stored_infra(sessions, data_dir=data_dir, base=base)
        except Exception as exc:  # a locked or briefly unreadable database: look again later
            log.warning("couldn't read the settings: %s", exc)
            continue
        if wanted != running:
            return


async def run_scheduled_cycle(poller: Poller, job: str, sources: tuple[str, ...]) -> float | None:
    """One scheduled cycle, logged; how soon to run again when it idled (no onboarded
    profile yet), so the first baseline starts about a minute after onboarding."""
    report = await poller.run_cycle(sources=sources)
    for line in render_report(report):
        log.info("%s: %s", job, line)
    return IDLE_RECHECK_SECONDS if report.idle else None


async def _keep_lease(
    sessions: sessionmaker[Session], me: lease.Holder, *, sleep: Sleep = asyncio.sleep
) -> None:
    """Renew the lease every minute; raise ``LeaseLost`` when another worker took it."""
    while True:
        await sleep(lease.RENEW_EVERY.total_seconds())
        with session_scope(sessions) as session:
            if not lease.renew(session, me, now=utcnow()):
                raise LeaseLost(
                    "another worker took over this database's lease; stopping so nothing is "
                    "pushed twice"
                )


def scheduled_jobs(
    config: InfraConfig, run: Callable[[str, tuple[str, ...]], Awaitable[float | None]]
) -> dict[str, tuple[Cadence, JobFunc]]:
    """One job per source group, each on its own cadence: the watchlist boards, the Simplify
    feed when enabled, and the job-board searches. Searches live in the profile and can be
    added while the worker runs, so their job is always scheduled (with none, its cycle
    fetches nothing)."""
    cadences = cadences_from_config(config)
    groups: list[tuple[str, tuple[str, ...]]] = [("watchlist", WATCHLIST_SOURCES)]
    if config.simplify.enabled:
        groups.append(("simplify", FEED_SOURCES))
    groups.append(("job_boards", BOARD_SOURCES))

    def job(name: str, sources: tuple[str, ...]) -> JobFunc:
        async def poll() -> float | None:
            return await run(name, sources)

        return poll

    return {name: (cadences[name], job(name, sources)) for name, sources in groups}


def _count(number: int, singular: str, plural: str | None = None) -> str:
    return f"{number} {singular if number == 1 else plural or singular + 's'}"


def _interval(seconds: float) -> str:
    return f"{seconds:.0f} s" if seconds < 60 else f"{seconds / 60:.0f} min"


def _describe(name: str, schedule: Cadence) -> str:
    return f"{name} every {_interval(schedule.interval_seconds)} ± {schedule.jitter_seconds:.0f} s"


async def send_test_notification(*, store: SecretStore, database_url: str) -> list[str]:
    """One test push through every channel that's set up; their names (none: nothing sent)."""
    sessions = open_database(database_url)
    async with build_http(stored_infra(sessions)) as http:
        return await send_test(LiveNotifier(http, sessions, store))
