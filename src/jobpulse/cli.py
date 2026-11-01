"""jobpulse command-line interface (Typer)."""

import asyncio
import ipaddress
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import uvicorn

from jobpulse import __version__
from jobpulse.board_import import ImportReport, import_boards, read_links, render_import
from jobpulse.config import (
    DEFAULT_ENV_FILE,
    AppConfig,
    ConfigError,
    Settings,
    clean_keywords,
    company_blocklist,
    config_warnings,
    dashboard_credentials,
    load_config,
    load_settings,
    require_secrets,
    resolve_config_path,
)
from jobpulse.core.filters import Blocklist
from jobpulse.core.timeparse import utcnow
from jobpulse.db import migrate, repo
from jobpulse.db.session import display_url, resolve_database_url, session_scope
from jobpulse.devtools.fixtures import FixtureScraper, FixtureTransport
from jobpulse.logging_setup import configure_logging
from jobpulse.runtime import (
    KNOWN_SOURCES,
    DatabaseNotReady,
    WorkerStalled,
    build_http,
    open_database,
    poll_once,
    render_report,
    run_worker,
    send_test_notification,
)
from jobpulse.sources.ats_detect import AtsDetector
from jobpulse.web.app import create_app

log = logging.getLogger("jobpulse")

app = typer.Typer(
    name="jobpulse",
    help="Personal job-posting watcher: finds fresh postings and notifies you. It never applies.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
config_app = typer.Typer(help="Validate configuration.", no_args_is_help=True)
db_app = typer.Typer(help="Database migrations.", no_args_is_help=True)
companies_app = typer.Typer(help="Watchlist companies.", no_args_is_help=True)
notify_app = typer.Typer(help="Notification channels.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(companies_app, name="companies")
app.add_typer(notify_app, name="notify")


@dataclass(frozen=True)
class CliState:
    config_path: Path
    env_file: Path
    settings: Settings


def _fail(error: object) -> NoReturn:
    typer.echo(f"jobpulse: {error}", err=True)
    raise typer.Exit(code=2)


def _state(ctx: typer.Context) -> CliState:
    state = ctx.obj
    if not isinstance(state, CliState):
        raise RuntimeError("CLI state missing; commands must run through the main app")
    return state


def _load_config(state: CliState) -> AppConfig:
    try:
        return load_config(state.config_path)
    except ConfigError as exc:
        _fail(exc)


def _database_url(state: CliState, config: AppConfig | None = None) -> str:
    """Config is optional here: without config.yaml, DATABASE_URL or the default applies."""
    if config is None and state.config_path.exists():
        config = _load_config(state)
    return resolve_database_url(state.settings, config)


def _require_secrets(state: CliState, config: AppConfig, *, llm: bool = False) -> None:
    """``llm``: the command may call the LLM layer (the worker, a live poll)."""
    try:
        require_secrets(config, state.settings, env_file=state.env_file, llm=llm)
    except ConfigError as exc:
        _fail(exc)


def _start_logging(state: CliState, config: AppConfig) -> None:
    configure_logging(state.settings.log_level, secrets=state.settings.secret_values())
    for warning in config_warnings(config):
        log.warning("config: %s", warning)


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"jobpulse {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="config.yaml to use (default: $JOBPULSE_CONFIG, then ./config.yaml).",
        ),
    ] = None,
    env_file: Annotated[
        Path, typer.Option("--env-file", help="File with secrets such as NTFY_TOPIC.")
    ] = DEFAULT_ENV_FILE,
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_show_version, is_eager=True, help="Show the version and exit."
        ),
    ] = False,
) -> None:
    try:
        settings = load_settings(env_file)
    except ConfigError as exc:
        _fail(exc)
    ctx.obj = CliState(
        config_path=resolve_config_path(config, settings), env_file=env_file, settings=settings
    )


@config_app.command("check")
def config_check(ctx: typer.Context) -> None:
    """Validate config.yaml and the secrets your notification channels (and the LLM layer,
    when it's on) need."""
    state = _state(ctx)
    config = _load_config(state)
    try:
        require_secrets(config, state.settings, env_file=state.env_file, llm=True)
    except ConfigError as exc:
        _fail(exc)
    for warning in config_warnings(config):
        typer.echo(f"warning: {warning}", err=True)
    channels = ", ".join(config.notify.channels) or "none"
    typer.echo(
        f"{state.config_path}: OK ({len(config.watchlist)} companies, "
        f"{len(config.searches)} searches; channels: {channels})"
    )


@db_app.command("upgrade")
def db_upgrade(
    ctx: typer.Context,
    revision: Annotated[str, typer.Argument(help="Target revision.")] = "head",
) -> None:
    """Apply migrations, up to the latest by default."""
    url = _database_url(_state(ctx))
    try:
        migrate.upgrade(url, revision)
    except FileNotFoundError as exc:
        _fail(exc)
    typer.echo(f"{display_url(url)}: upgraded to {revision}")


@db_app.command("current")
def db_current(ctx: typer.Context) -> None:
    """Show the database's current migration revision."""
    try:
        migrate.show_current(_database_url(_state(ctx)))
    except FileNotFoundError as exc:
        _fail(exc)


@app.command()
def poll(
    ctx: typer.Context,
    once: Annotated[
        bool,
        typer.Option("--once", help="Run one cycle and exit (the worker runs them on a schedule)."),
    ] = True,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Fetch and evaluate in a transaction that is rolled back; send nothing.",
        ),
    ] = False,
    source: Annotated[
        list[str] | None,
        typer.Option(
            "--source",
            help="Only this source (repeatable): greenhouse, lever, ashby, workday, simplify, "
            "indeed, linkedin.",
        ),
    ] = None,
    target: Annotated[
        str | None, typer.Option("--target", help="Only this company token or search name.")
    ] = None,
    fixtures: Annotated[
        Path | None,
        typer.Option(
            "--fixtures",
            help="Development: answer HTTP and JobSpy from recorded responses (DIR/routes.yaml).",
            exists=True,
            file_okay=False,
        ),
    ] = None,
) -> None:
    """Run one poll cycle over every source (or the --source ones)."""
    state = _state(ctx)
    config = _load_config(state)
    unknown = sorted(set(source or ()) - set(KNOWN_SOURCES))
    if unknown:
        _fail(f"unknown source {', '.join(unknown)}; choose from {', '.join(KNOWN_SOURCES)}")
    if not dry_run:
        _require_secrets(state, config, llm=True)
    _start_logging(state, config)
    try:
        report = asyncio.run(
            poll_once(
                config=config,
                settings=state.settings,
                database_url=_database_url(state, config),
                dry_run=dry_run,
                sources=source or None,
                target=target,
                transport=FixtureTransport(fixtures) if fixtures else None,
                scrape=FixtureScraper(fixtures) if fixtures else None,
            )
        )
    except (DatabaseNotReady, ConfigError) as exc:  # ConfigError: the resume went missing
        _fail(exc)
    for line in render_report(report):
        typer.echo(line)


@app.command()
def worker(ctx: typer.Context) -> None:
    """Run the scheduler: poll every source on its own interval (Ctrl-C to stop)."""
    state = _state(ctx)
    config = _load_config(state)
    _require_secrets(state, config, llm=True)
    _start_logging(state, config)
    try:
        asyncio.run(
            run_worker(
                config=config, settings=state.settings, database_url=_database_url(state, config)
            )
        )
    except (DatabaseNotReady, ConfigError) as exc:  # ConfigError: the resume went missing
        _fail(exc)
    except WorkerStalled as exc:
        log.error("%s", exc)
        raise typer.Exit(code=1) from None


@companies_app.command("list")
def companies_list(ctx: typer.Context) -> None:
    """List watchlist companies with their baseline state and open postings."""
    state = _state(ctx)
    config = _load_config(state) if state.config_path.exists() else None
    blocklist = company_blocklist(config) if config else Blocklist()
    try:
        sessions = open_database(_database_url(state, config))
    except DatabaseNotReady as exc:
        _fail(exc)
    with session_scope(sessions) as session:
        companies = repo.list_companies(session)
        blocklist = repo.blocklist(session, blocklist)  # with the dashboard's blocks
    if not companies:
        typer.echo("no companies yet; run `jobpulse companies sync` or `jobpulse poll --once`")
    for company in companies:
        baseline = (
            f"baseline {company.baseline_done_at:%Y-%m-%d}"
            if company.baseline_done_at
            else "baseline pending"
        )
        blocked = blocklist.blocks_board(company.ats, company.token) or blocklist.blocks_company(
            company.name
        )
        status = "inactive" if not company.active else "blocked" if blocked else "active"
        typer.echo(
            f"{company.ats:<10} {company.token:<24} {company.name:<24} priority {company.priority:<3}"
            f" {status:<8} {baseline:<20} {company.open_postings} open ({company.source_of_discovery})"
        )


@companies_app.command("sync")
def companies_sync(ctx: typer.Context) -> None:
    """Upsert the watchlist from config.yaml. Every poll does this too."""
    state = _state(ctx)
    config = _load_config(state)
    try:
        sessions = open_database(_database_url(state, config))
    except DatabaseNotReady as exc:
        _fail(exc)
    with session_scope(sessions) as session:
        report = repo.sync_watchlist(session, config.watchlist, now=utcnow())
    for label, names in (
        ("added", report.added),
        ("updated", report.updated),
        ("deactivated", report.deactivated),
    ):
        for name in names:
            typer.echo(f"{label}: {name}")
    if not (report.added or report.updated or report.deactivated):
        typer.echo("watchlist already in sync")


@companies_app.command("import")
def companies_import(
    ctx: typer.Context,
    file: Annotated[
        Path,
        typer.Argument(
            help="A file with job or careers links: JSON (every URL value; a company name "
            "beside it names the board) or any text.",
            exists=True,
            dir_okay=False,
        ),
    ],
    workday_keywords: Annotated[
        str,
        typer.Option(
            "--workday-keywords",
            help="Comma-separated keywords each Workday board is searched for every poll, e.g. "
            '"software engineer, full stack". Without them, Workday boards are left out.',
        ),
    ] = "",
    priority: Annotated[
        int,
        typer.Option("--priority", min=0, max=1000, help="Priority of the added companies."),
    ] = 0,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Find and validate the boards; add nothing.")
    ] = False,
) -> None:
    """Add the Greenhouse, Lever, Ashby and Workday boards behind a file's links to the
    watchlist, each validated like a URL pasted on the Watchlist page."""
    state = _state(ctx)
    config = _load_config(state)
    try:
        keywords = clean_keywords(workday_keywords.split(","))
    except ValueError as exc:
        _fail(exc)
    try:
        links = read_links(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        _fail(f"couldn't read {file}: {exc}")
    try:
        sessions = open_database(_database_url(state, config))
    except DatabaseNotReady as exc:
        _fail(exc)
    _start_logging(state, config)

    async def run() -> ImportReport:
        async with build_http(config) as http:
            return await import_boards(
                links,
                detector=AtsDetector(http),
                sessions=sessions,
                config=config,
                keywords=keywords,
                priority=priority,
                dry_run=dry_run,
            )

    for line in render_import(asyncio.run(run()), config=config):
        typer.echo(line)


@notify_app.command("test")
def notify_test(ctx: typer.Context) -> None:
    """Send one test push through every configured channel."""
    state = _state(ctx)
    config = _load_config(state)
    _require_secrets(state, config)
    _start_logging(state, config)
    channels = asyncio.run(send_test_notification(config=config, settings=state.settings))
    if not channels:
        _fail("no notification channels configured (notify.channels in config.yaml)")
    typer.echo(f"test notification sent via {', '.join(channels)}")


@app.command()
def web(
    ctx: typer.Context,
    host: Annotated[
        str | None,
        typer.Option("--host", help="Address to listen on (default: dashboard.host, 127.0.0.1)."),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option("--port", help="Port (default: dashboard.port, 8000).", min=1, max=65535),
    ] = None,
) -> None:
    """Serve the dashboard (http://127.0.0.1:8000 by default)."""
    state = _state(ctx)
    config = _load_config(state)
    try:
        credentials = dashboard_credentials(state.settings, env_file=state.env_file)
    except ConfigError as exc:
        _fail(exc)
    _start_logging(state, config)
    try:
        sessions = open_database(_database_url(state, config))
    except DatabaseNotReady as exc:
        _fail(exc)
    bind_host = host or config.dashboard.host
    bind_port = port or config.dashboard.port
    if credentials is None and not _is_loopback(bind_host):
        log.warning(
            "the dashboard listens on %s with no password: anyone who can reach port %d can "
            "use it. Set DASHBOARD_USER and DASHBOARD_PASSWORD, or publish the port on "
            "127.0.0.1 only (as docker-compose.yml does)",
            bind_host,
            bind_port,
        )
    application = create_app(
        config=config,
        sessions=sessions,
        credentials=credentials,
        secrets=state.settings.secret_values(),
    )
    log.info(
        "dashboard on http://%s:%d/ (basic auth %s)",
        bind_host,
        bind_port,
        "on" if credentials else "off",
    )
    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, _SkipProbes) for item in access_log.filters):
        access_log.addFilter(_SkipProbes())
    uvicorn.run(application, host=bind_host, port=bind_port, log_config=None, server_header=False)


class _SkipProbes(logging.Filter):
    """Docker's health check calls /healthz every 30 s; those lines are only noise."""

    def filter(self, record: logging.LogRecord) -> bool:
        return " /healthz " not in record.getMessage()


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
