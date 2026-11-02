"""jobpulse command-line interface (Typer)."""

import asyncio
import ipaddress
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import uvicorn
import yaml
from sqlalchemy.orm import Session, sessionmaker

from jobpulse import __version__
from jobpulse.board_import import ImportReport, import_boards, read_links, render_import
from jobpulse.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_ENV_FILE,
    MOVED_KEYS,
    AppConfig,
    ConfigError,
    InfraConfig,
    Profile,
    Settings,
    clean_keywords,
    company_blocklist,
    compose,
    config_warnings,
    dashboard_credentials,
    filter_rules,
    load_config,
    load_legacy_config,
    load_settings,
    profile_yaml,
    read_config_lenient,
    require_secrets,
    resolve_config_path,
    scoring_weights,
    split,
)
from jobpulse.core.similarity import resume_lines
from jobpulse.core.timeparse import utcnow
from jobpulse.credentials import SecretStore
from jobpulse.db import migrate, repo
from jobpulse.db import profile as profile_db
from jobpulse.db import resume as resume_db
from jobpulse.db.lease import LeaseHeld
from jobpulse.db.session import display_url, resolve_database_url, session_scope
from jobpulse.devtools.fixtures import FixtureScraper, FixtureTransport
from jobpulse.embeddings.base import EmbedderUnavailable
from jobpulse.embeddings.fastembed import DEFAULT_CACHE_DIR, DEFAULT_MODEL, download_model
from jobpulse.logging_setup import SecretRegistry, configure_logging
from jobpulse.poller import NOT_ONBOARDED
from jobpulse.resume_files import ResumeFileError, read_resume_file
from jobpulse.resume_scoring import (
    backfill,
    missing_vectors,
    refresh_resume,
    rescore,
    restore_stack,
)
from jobpulse.runtime import (
    KNOWN_SOURCES,
    DatabaseNotReady,
    LeaseLost,
    WorkerStalled,
    build_embedder,
    build_http,
    open_database,
    poll_once,
    render_report,
    run_worker,
    send_test_notification,
)
from jobpulse.seed import (
    ExportReport,
    Seed,
    SeedError,
    SeedPlan,
    contribution,
    export_seed,
    packaged_seed,
    parse_extra,
    parse_seed,
    plan_seed,
    render_seed,
    retirable,
    seed_keywords,
    write_seed,
)
from jobpulse.sources.ats_detect import AtsDetector
from jobpulse.up import run_app
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
seed_app = typer.Typer(help="The board seed every install starts from.", no_args_is_help=True)
notify_app = typer.Typer(help="Notification channels.", no_args_is_help=True)
embeddings_app = typer.Typer(help="The local embedding model.", no_args_is_help=True)
resume_app = typer.Typer(
    help="The resume postings are compared with (in place of stack keywords).",
    no_args_is_help=True,
)
profile_app = typer.Typer(
    help="Your preferences (filters, scoring, notifications, searches), kept in the database.",
    no_args_is_help=True,
)
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(companies_app, name="companies")
companies_app.add_typer(seed_app, name="seed")
app.add_typer(notify_app, name="notify")
app.add_typer(embeddings_app, name="embeddings")
app.add_typer(resume_app, name="resume")
app.add_typer(profile_app, name="profile")


@dataclass(frozen=True)
class CliState:
    config_path: Path
    env_file: Path
    settings: Settings
    store: SecretStore
    config_explicit: bool = False  # --config or JOBPULSE_CONFIG: the file must exist

    @property
    def registry(self) -> SecretRegistry:
        return self.store.registry


def _fail(error: object) -> NoReturn:
    typer.echo(f"jobpulse: {error}", err=True)
    raise typer.Exit(code=2)


def _state(ctx: typer.Context) -> CliState:
    state = ctx.obj
    if not isinstance(state, CliState):
        raise RuntimeError("CLI state missing; commands must run through the main app")
    return state


def _load_infra(state: CliState) -> InfraConfig:
    """config.yaml (infrastructure only); without the default ./config.yaml, the defaults."""
    try:
        return load_config(state.config_path, missing_ok=not state.config_explicit)
    except ConfigError as exc:
        _fail(exc)


def _database_url(state: CliState, infra: InfraConfig | None = None) -> str:
    """Read leniently when no config was loaded: an old config.yaml still names the database."""
    if infra is None:
        try:
            infra = read_config_lenient(state.config_path, missing_ok=not state.config_explicit)
        except ConfigError as exc:
            _fail(exc)
    return resolve_database_url(state.settings, infra)


def _sessions(state: CliState, infra: InfraConfig | None = None) -> sessionmaker[Session]:
    try:
        return open_database(_database_url(state, infra))
    except DatabaseNotReady as exc:
        _fail(exc)


def _app_config(
    state: CliState, infra: InfraConfig, sessions: sessionmaker[Session], *, required: bool = True
) -> AppConfig:
    """config.yaml composed with the stored profile. ``required``: a command that polls
    or sends needs an onboarded, valid profile; others fall back to the defaults."""
    with sessions() as session:
        stored = profile_db.get(session)
    if stored.problem is not None and required:
        _fail(f"{stored.problem}; fix it on Settings")
    if (stored.profile is None or not stored.onboarded) and required:
        _fail(NOT_ONBOARDED)
    return compose(infra, stored.profile or Profile())


def _require_secrets(
    state: CliState, config: AppConfig, sessions: sessionmaker[Session], *, llm: bool = False
) -> None:
    """``llm``: the command may call the LLM layer (a live poll)."""
    with sessions() as session:
        secrets = state.store.resolve(session)
    try:
        require_secrets(config, secrets.values, env_file=state.env_file, llm=llm)
    except ConfigError as exc:
        _fail(exc)


def _start_logging(state: CliState, config: AppConfig | None = None) -> None:
    configure_logging(state.settings.log_level, secrets=state.registry)
    for warning in config_warnings(config) if config is not None else ():
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
    store = SecretStore(settings, SecretRegistry(settings.secret_values()))
    ctx.obj = CliState(
        config_path=resolve_config_path(config, settings),
        env_file=env_file,
        settings=settings,
        store=store,
        config_explicit=config is not None or settings.jobpulse_config is not None,
    )


@config_app.command("check")
def config_check(ctx: typer.Context) -> None:
    """Validate config.yaml, the stored profile, and the secrets your notification channels
    (and the LLM layer, when it's on) need."""
    state = _state(ctx)
    infra = _load_infra(state)
    sessions = _sessions(state, infra)
    config = _app_config(state, infra, sessions)
    _require_secrets(state, config, sessions, llm=True)
    for warning in config_warnings(config):
        typer.echo(f"warning: {warning}", err=True)
    with sessions() as session:
        companies = len(repo.list_companies(session))
        revision = profile_db.revision(session)
    channels = ", ".join(config.notify.channels) or "none"
    source = state.config_path if state.config_path.exists() else "no config.yaml (defaults)"
    typer.echo(
        f"{source}: OK; profile revision {revision} ({companies} companies, "
        f"{len(config.searches)} searches; channels: {channels})"
    )


@db_app.command("upgrade")
def db_upgrade(
    ctx: typer.Context,
    revision: Annotated[str, typer.Argument(help="Target revision.")] = "head",
) -> None:
    """Apply migrations, up to the latest by default."""
    url = _database_url(_state(ctx))
    migrate.upgrade(url, revision)
    typer.echo(f"{display_url(url)}: upgraded to {revision}")


@db_app.command("current")
def db_current(ctx: typer.Context) -> None:
    """Show the database's current migration revision."""
    migrate.show_current(_database_url(_state(ctx)))


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
    infra = _load_infra(state)
    unknown = sorted(set(source or ()) - set(KNOWN_SOURCES))
    if unknown:
        _fail(f"unknown source {', '.join(unknown)}; choose from {', '.join(KNOWN_SOURCES)}")
    sessions = _sessions(state, infra)
    config = _app_config(state, infra, sessions)
    if not dry_run:
        _require_secrets(state, config, sessions, llm=True)
    _start_logging(state, config)
    try:
        report = asyncio.run(
            poll_once(
                infra=infra,
                store=state.store,
                database_url=_database_url(state, infra),
                dry_run=dry_run,
                sources=source or None,
                target=target,
                transport=FixtureTransport(fixtures) if fixtures else None,
                scrape=FixtureScraper(fixtures) if fixtures else None,
            )
        )
    except DatabaseNotReady as exc:
        _fail(exc)
    for line in render_report(report):
        typer.echo(line)


@app.command()
def worker(ctx: typer.Context) -> None:
    """Run the scheduler: poll every source on its own interval (Ctrl-C to stop). Until a
    profile is stored, its cycles wait; notification secrets are read when a push goes out."""
    state = _state(ctx)
    infra = _load_infra(state)
    sessions = _sessions(state, infra)
    _start_logging(state, _app_config(state, infra, sessions, required=False))
    try:
        asyncio.run(
            run_worker(infra=infra, store=state.store, database_url=_database_url(state, infra))
        )
    except (DatabaseNotReady, LeaseHeld) as exc:
        _fail(exc)
    except (WorkerStalled, LeaseLost) as exc:
        log.error("%s", exc)
        raise typer.Exit(code=1) from None


@companies_app.command("list")
def companies_list(ctx: typer.Context) -> None:
    """List watchlist companies with their baseline state and open postings."""
    state = _state(ctx)
    infra = _load_infra(state)
    sessions = _sessions(state, infra)
    config = _app_config(state, infra, sessions, required=False)
    with session_scope(sessions) as session:
        companies = repo.list_companies(session)
        blocklist = repo.blocklist(  # with the dashboard's
            session, company_blocklist(config), agencies=config.hide_agencies
        )
    if not companies:
        typer.echo(
            "no companies yet; add boards on the Watchlist page, or run `jobpulse profile import`"
        )
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
    infra = _load_infra(state)
    try:
        keywords = clean_keywords(workday_keywords.split(","))
    except ValueError as exc:
        _fail(exc)
    try:
        links = read_links(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        _fail(f"couldn't read {file}: {exc}")
    sessions = _sessions(state, infra)
    config = _app_config(state, infra, sessions, required=False)
    _start_logging(state, config)

    async def run() -> ImportReport:
        async with build_http(infra, contact_email=config.contact_email) as http:
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


def _read_seed(path: Path | None) -> Seed:
    try:
        return packaged_seed() if path is None else parse_seed(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        _fail(f"couldn't read {path}: {exc}")
    except SeedError as exc:
        _fail(f"{path or 'the packaged seed'}: {exc}")


@seed_app.command("apply")
def seed_apply(
    ctx: typer.Context,
    file: Annotated[
        Path | None,
        typer.Option(
            "--file", help="A seed file to apply instead of the packaged one.", dir_okay=False
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Say what would change; change nothing.")
    ] = False,
) -> None:
    """Add the seed's boards you don't have yet (their first poll is silent) and pause the
    seed boards it removed, unless you edited them. Workday boards are searched once per
    keyword to keep only those that fit what a poll reads."""
    state = _state(ctx)
    infra = _load_infra(state)
    seed = _read_seed(file)
    sessions = _sessions(state, infra)
    config = _app_config(state, infra, sessions, required=False)
    _start_logging(state, config)
    with sessions() as session:
        known = repo.known_companies(session)
        blocklist = repo.blocklist(
            session, company_blocklist(config), agencies=config.hide_agencies
        )
    rules = replace(filter_rules(config), blocklist=blocklist)

    async def run() -> SeedPlan:
        async with build_http(infra, contact_email=config.contact_email) as http:
            return await plan_seed(
                seed,
                known=known,
                blocklist=blocklist,
                config=config,
                rules=rules,
                detector=AtsDetector(http),
                keywords=seed_keywords(config),
            )

    plan = asyncio.run(run())
    if dry_run:
        added = [result.board.label for result in plan.add]
        with sessions() as session:
            retired = retirable(session, plan)
    else:
        with session_scope(sessions) as session:
            added, retired = write_seed(session, plan, now=utcnow())
    would = "would " if dry_run else ""
    for label in added:
        typer.echo(f"{would}add {label}")
    for label in retired:
        typer.echo(f"{would}pause {label} (removed from the seed)")
    for result in plan.results:
        if result.outcome != "add":
            detail = f": {result.detail}" if result.detail else ""
            typer.echo(
                f"skipped {result.board.label} ({result.board.name}), {result.outcome}{detail}"
            )
    blocked = sum(result.outcome == "blocked" for result in plan.results)
    left_out = sum(result.outcome in ("invalid", "too broad") for result in plan.results)
    typer.echo(
        f"{len(seed.boards):,} boards in the seed: {len(added):,} "
        f"{'to add' if dry_run else 'added'}, {plan.known:,} already on your watchlist, "
        f"{blocked:,} blocked, {left_out:,} left out; {len(retired):,} "
        f"{'to pause' if dry_run else 'paused'}."
    )
    if added and not dry_run:
        typer.echo("Their first poll stores the jobs they have now without notifying you.")


def _write_or_print(text: str, out: Path | None) -> None:
    if out is None:
        typer.echo(text, nl=False)
        return
    try:
        out.write_text(text, encoding="utf-8")
    except OSError as exc:
        _fail(f"couldn't write {out}: {exc}")


@seed_app.command("export")
def seed_export(
    ctx: typer.Context,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Where to write the seed (its current content is the previous seed). "
            "Without it, the seed goes to standard output.",
            dir_okay=False,
        ),
    ] = None,
    add: Annotated[
        list[str] | None,
        typer.Option(
            "--add",
            help="A board to add, as ats/token or ats/token=Name (e.g. "
            "greenhouse/anthropic=Anthropic); validated once. Repeat it for more.",
        ),
    ] = None,
) -> None:
    """Build the seed from your watchlist: active boards less staffing agencies and boards
    whose latest run was a 4xx, without priorities or keywords."""
    state = _state(ctx)
    infra = _load_infra(state)
    try:
        extra = [parse_extra(value) for value in add or ()]
    except ValueError as exc:
        _fail(exc)
    previous = _read_seed(out if out is not None and out.exists() else None)
    sessions = _sessions(state, infra)
    config = _app_config(state, infra, sessions, required=False)
    _start_logging(state, config)

    async def run() -> ExportReport:
        async with build_http(infra, contact_email=config.contact_email) as http:
            return await export_seed(
                sessions=sessions, previous=previous, extra=extra, detector=AtsDetector(http)
            )

    report = asyncio.run(run())
    _write_or_print(render_seed(report.seed), out)
    notes = [
        *(f"left out (staffing agency): {line}" for line in report.agencies),
        *(f"left out (latest run a 4xx): {line}" for line in report.dead),
        *(f"added: {line}" for line in report.extra_added),
        *(f"already in the seed: {line}" for line in report.extra_known),
        *(f"couldn't validate {line}" for line in report.extra_invalid),
        *(f"review (looks like an agency): {line}" for line in report.flagged),
        f"{len(report.seed.boards):,} boards and {len(report.seed.removed):,} removed in the "
        f"seed ({report.carried:,} kept from the previous seed though not active here).",
    ]
    for note in notes:
        typer.echo(note, err=True)


CONTRIBUTE_STEPS = """To contribute them:
  1. Fork https://github.com/aismael234/jobpulse and clone your fork.
  2. Add these boards to src/jobpulse/data/seed/boards.json, one per line, keeping the list
     sorted by ats and token.
  3. Run `uv run pytest tests/unit/test_seed_file.py`, which checks the file.
  4. Open a pull request.
"""


@companies_app.command("contribute")
def companies_contribute(
    ctx: typer.Context,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out", help="Where to write the boards; standard output without it.", dir_okay=False
        ),
    ] = None,
) -> None:
    """Write the active boards you added that the seed lacks, in the seed's format, with the
    steps to send them as a pull request."""
    state = _state(ctx)
    infra = _load_infra(state)
    seed = _read_seed(None)
    sessions = _sessions(state, infra)
    with sessions() as session:
        mine = contribution(session, seed)
    if not mine.boards:
        typer.echo("nothing to contribute: the seed has every board on your watchlist", err=True)
        return
    _write_or_print(render_seed(mine), out)
    count = len(mine.boards)
    where = f" in {out}" if out is not None else ""
    typer.echo(f"{count:,} board{'s' if count != 1 else ''} the seed lacks{where}.", err=True)
    typer.echo(CONTRIBUTE_STEPS, err=True, nl=False)


@notify_app.command("test")
def notify_test(ctx: typer.Context) -> None:
    """Send one test push through every configured channel."""
    state = _state(ctx)
    infra = _load_infra(state)
    sessions = _sessions(state, infra)
    config = _app_config(state, infra, sessions)
    _require_secrets(state, config, sessions)
    _start_logging(state, config)
    channels = asyncio.run(
        send_test_notification(
            infra=infra, store=state.store, database_url=_database_url(state, infra)
        )
    )
    if not channels:
        _fail("no notification channels configured (notify.channels, on Settings)")
    typer.echo(f"test notification sent via {', '.join(channels)}")


@embeddings_app.command("download")
def embeddings_download(
    cache_dir: Annotated[
        Path, typer.Option("--cache-dir", help="Where to keep the model.")
    ] = DEFAULT_CACHE_DIR,
) -> None:
    """Download the embedding model from Hugging Face (the only command that downloads it)."""
    typer.echo(f"Downloading {DEFAULT_MODEL} into {cache_dir} ...")
    try:
        model = download_model(DEFAULT_MODEL, cache_dir)
    except Exception as exc:  # network, disk or Hugging Face errors: report, don't trace
        _fail(f"download failed ({type(exc).__name__}: {exc})")
    typer.echo(f"{model} is ready in {cache_dir}")


def _plural(number: int, singular: str, plural: str) -> str:
    return f"{number} {singular if number == 1 else plural}"


@resume_app.command("set")
def resume_set(
    ctx: typer.Context,
    file: Annotated[
        Path,
        typer.Argument(help="Your resume: .pdf, .docx, .txt or .md, at most 2 MB.", dir_okay=False),
    ],
) -> None:
    """Store a resume, replacing any other. Postings are compared with its content lines;
    headings and contact details are left out."""
    state = _state(ctx)
    try:
        read = read_resume_file(file)
    except ResumeFileError as exc:
        _fail(exc)
    lines = resume_lines(read.text)
    if not lines:
        _fail(f"no content lines found in {file.name}: use bullets or full sentences")
    with session_scope(_sessions(state)) as session:
        resume_db.put(
            session,
            filename=file.name,
            media_type=read.media_type,
            text=read.text,
            lines=lines,
            now=utcnow(),
        )
    typer.echo(f"{file.name}: {_plural(len(lines), 'content line', 'content lines')} stored.")
    typer.echo(
        "The worker embeds it and re-scores open matches on its next cycle; to do it now, run "
        "`jobpulse resume rescore --backfill`."
    )


@resume_app.command("show")
def resume_show(
    ctx: typer.Context,
    lines: Annotated[bool, typer.Option("--lines", help="List the content lines too.")] = False,
) -> None:
    """Show the stored resume: file, content lines, model and the last re-score."""
    with session_scope(_sessions(_state(ctx))) as session:
        stored = resume_db.get(session)
    if stored is None:
        typer.echo("no resume stored; run `jobpulse resume set FILE`")
        return
    typer.echo(
        f"{stored.filename} ({stored.media_type}), set {stored.uploaded_at:%Y-%m-%d %H:%M} UTC: "
        f"{_plural(len(stored.lines), 'content line', 'content lines')}"
    )
    if stored.embed_model is None:
        typer.echo("not embedded yet: the worker does it on its next cycle")
    else:
        typer.echo(f"embedded with {stored.embed_model}")
    if stored.rescored_at is not None:
        low, high = stored.anchors
        measured = (
            f"measured on {stored.anchor_count} matches" if stored.anchor_count else "defaults"
        )
        typer.echo(
            f"open matches re-scored {stored.rescored_at:%Y-%m-%d %H:%M} UTC; "
            f"anchors {low:.3f}-{high:.3f} ({measured})"
        )
    if lines:
        for line in stored.lines:
            typer.echo(f"  - {line}")


@resume_app.command("clear")
def resume_clear(ctx: typer.Context) -> None:
    """Delete the resume; open matches go back to the stack keywords."""
    state = _state(ctx)
    infra = _load_infra(state)
    sessions = _sessions(state, infra)
    config = _app_config(state, infra, sessions, required=False)
    with session_scope(sessions) as session:
        if not resume_db.clear(session):
            typer.echo("no resume stored")
            return
        restored = restore_stack(
            session, rules=filter_rules(config), weights=scoring_weights(config)
        )
    typer.echo(
        f"resume cleared; {_plural(restored, 'open match', 'open matches')} back on stack keywords"
    )


@resume_app.command("rescore")
def resume_rescore(
    ctx: typer.Context,
    fill: Annotated[
        bool,
        typer.Option(
            "--backfill",
            help="First embed open matches whose description has no vector yet (about 16 a "
            "second).",
        ),
    ] = False,
) -> None:
    """Re-score open New and Saved matches against the stored resume now, instead of at the
    worker's next cycle."""
    state = _state(ctx)
    infra = _load_infra(state)
    embedder = build_embedder(infra)
    if embedder is None:
        _fail("resume similarity is off (embeddings.enabled is false, or fastembed is missing)")
    sessions = _sessions(state, infra)
    config = _app_config(state, infra, sessions, required=False)
    with session_scope(sessions) as session:
        stored = resume_db.get(session)
        if stored is None or not stored.lines:
            _fail("no resume stored; run `jobpulse resume set FILE` first")
        try:
            if refresh_resume(session, embedder):
                typer.echo(f"embedded the resume with {embedder.model}")
            session.commit()
            if fill:

                def progress(done: int) -> None:
                    session.commit()
                    typer.echo(f"  {done} embedded")

                count = backfill(session, embedder, now=utcnow(), progress=progress)
                typer.echo(f"embedded {_plural(count, 'description', 'descriptions')}")
            elif missing := len(missing_vectors(session, embedder.model)):
                typer.echo(
                    f"{_plural(missing, 'open match has', 'open matches have')} no vector yet; "
                    "run with --backfill to embed them first"
                )
        except EmbedderUnavailable as exc:
            _fail(exc)
        result = rescore(
            session, rules=filter_rules(config), weights=scoring_weights(config), now=utcnow()
        )
    restored = (
        f"; {_plural(result.restored, 'match', 'matches')} back on stack keywords"
        if result.restored
        else ""
    )
    anchors = (
        f"anchors measured on {result.measured_on}" if result.measured_on else "default anchors"
    )
    typer.echo(
        f"re-scored {_plural(result.rescored, 'open match', 'open matches')} ({anchors}){restored}"
    )


@profile_app.command("import")
def profile_import(
    ctx: typer.Context,
    file: Annotated[
        Path | None,
        typer.Argument(
            help="A config.yaml from before preferences moved to the database (default: "
            "the --config file).",
            dir_okay=False,
        ),
    ] = None,
    replace: Annotated[
        bool, typer.Option("--replace", help="Overwrite a different profile already stored.")
    ] = False,
    write_infra: Annotated[
        Path | None,
        typer.Option(
            "--write-infra",
            help="Also write the file's infrastructure keys (schedules, pacing...) here: "
            "the config.yaml to use from now on.",
            dir_okay=False,
        ),
    ] = None,
) -> None:
    """Bring an old config.yaml's preferences into the database, once: the profile, the
    watchlist (edited on the Watchlist page from then on) and the blocked company names
    (unblockable there). Running it again changes nothing."""
    state = _state(ctx)
    path = file or state.config_path
    try:
        legacy = load_legacy_config(path)
    except ConfigError as exc:
        _fail(exc)
    if write_infra is not None and write_infra.exists():
        _fail(f"{write_infra} exists; choose a new path, so nothing is overwritten")
    infra, profile = split(legacy)
    names = list(profile.blocklist.companies)
    # The names become dashboard blocks; the profile keeps the boards.
    profile = profile.model_copy(
        update={"blocklist": profile.blocklist.model_copy(update={"companies": []})}
    )
    now = utcnow()
    with session_scope(_sessions(state, infra)) as session:
        stored = profile_db.get(session)
        if stored.profile == profile and stored.onboarded:
            typer.echo(f"profile: unchanged (revision {stored.revision})")
        elif stored.revision and not replace:
            _fail(
                f"a different profile is already stored (revision {stored.revision}); pass "
                "--replace to overwrite it, or edit it on Settings"
            )
        else:
            revision = profile_db.save(session, profile, now=now)
            typer.echo(f"profile: imported from {path} (revision {revision})")
        watchlist = repo.import_watchlist(session, legacy.watchlist, now=now)
        blocked = repo.import_blocklist(session, names, now=now)
    typer.echo(
        f"watchlist: {_plural(len(watchlist.added), 'board', 'boards')} added, "
        f"{len(watchlist.updated)} updated; edit them on the Watchlist page from now on"
    )
    typer.echo(
        f"blocklist: {_plural(len(blocked), 'company', 'companies')} newly blocked on the "
        f"dashboard ({len(names) - len(blocked)} already were)"
    )
    if write_infra is not None:
        write_infra.write_text(_infra_yaml(path), encoding="utf-8")
        typer.echo(f"infrastructure: wrote {write_infra}")
    elif path == DEFAULT_CONFIG_PATH or path == state.config_path:
        typer.echo(
            f"next: take the preference sections out of {path} (or rerun with --write-infra "
            "and use that file); until then every command but this one refuses it"
        )


def _infra_yaml(path: Path) -> str:
    """The infrastructure keys of an old config.yaml, as they were written there."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    kept = {key: value for key, value in data.items() if key not in MOVED_KEYS}
    header = (
        "# jobpulse infrastructure (config.yaml). Preferences are in the database: edit them on\n"
        "# Settings. Written by `jobpulse profile import`.\n"
    )
    return header + (yaml.safe_dump(kept, sort_keys=False, allow_unicode=True) if kept else "")


@profile_app.command("export")
def profile_export(ctx: typer.Context) -> None:
    """Print the stored profile as YAML (what Settings shows under Profile)."""
    with _sessions(_state(ctx))() as session:
        stored = profile_db.get(session)
    if stored.profile is None:
        _fail(stored.problem or "no profile stored; run `jobpulse profile import`")
    typer.echo(profile_yaml(stored.profile), nl=False)


@profile_app.command("show")
def profile_show(ctx: typer.Context) -> None:
    """Summarize the stored profile."""
    with _sessions(_state(ctx))() as session:
        stored = profile_db.get(session)
    if stored.revision == 0:
        typer.echo("no profile stored; run `jobpulse profile import`, or save one on Settings")
        return
    if stored.problem is not None or stored.profile is None:
        _fail(stored.problem or "the stored profile is invalid")
    profile = stored.profile
    saved = f"{stored.updated_at:%Y-%m-%d %H:%M} UTC" if stored.updated_at else "unknown"
    typer.echo(
        f"revision {stored.revision}, saved {saved}; "
        f"{'onboarded' if stored.onboarded else 'not onboarded (the worker waits)'}"
    )
    filters = profile.filters
    typer.echo(
        f"title rules: {len(filters.title.include)} include, {len(filters.title.exclude)} "
        f"exclude; stack keywords: {len(filters.description.stack_keywords)}; countries: "
        f"{', '.join(filters.locations.countries) or 'any'}"
    )
    typer.echo(
        f"searches: {len(profile.searches)}; channels: "
        f"{', '.join(profile.notify.channels) or 'none'}; llm: "
        f"{'on' if profile.llm.enabled else 'off'}; timezone: {profile.timezone}"
    )


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
    infra = _load_infra(state)
    try:
        credentials = dashboard_credentials(state.settings, env_file=state.env_file)
    except ConfigError as exc:
        _fail(exc)
    sessions = _sessions(state, infra)
    _start_logging(state, _app_config(state, infra, sessions, required=False))
    bind_host, bind_port = _bind(infra, host, port, credentials)
    application = create_app(
        infra=infra, sessions=sessions, credentials=credentials, store=state.store
    )
    log.info(
        "dashboard on http://%s:%d/ (basic auth %s)",
        bind_host,
        bind_port,
        "on" if credentials else "off",
    )
    _quiet_probes()
    uvicorn.run(application, host=bind_host, port=bind_port, log_config=None, server_header=False)


@app.command()
def up(
    ctx: typer.Context,
    host: Annotated[
        str | None,
        typer.Option("--host", help="Address to listen on (default: dashboard.host, 127.0.0.1)."),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option("--port", help="Port (default: dashboard.port, 8000).", min=1, max=65535),
    ] = None,
    no_migrate: Annotated[
        bool,
        typer.Option("--no-migrate", help="Don't back up and upgrade the database first."),
    ] = False,
) -> None:
    """Run everything in one process: back up and upgrade the database, then serve the
    dashboard with the scheduler alongside. A new install opens the setup wizard."""
    state = _state(ctx)
    infra = _load_infra(state)
    try:
        credentials = dashboard_credentials(state.settings, env_file=state.env_file)
    except ConfigError as exc:
        _fail(exc)
    url = _database_url(state, infra)
    _start_logging(state)
    bind_host, bind_port = _bind(infra, host, port, credentials)
    _quiet_probes()
    log.info(
        "jobpulse on http://%s:%d/ (basic auth %s)",
        bind_host,
        bind_port,
        "on" if credentials else "off",
    )
    try:
        code = run_app(
            infra=infra,
            store=state.store,
            database_url=url,
            host=bind_host,
            port=bind_port,
            credentials=credentials,
            migrate_first=not no_migrate,
        )
    except (DatabaseNotReady, LeaseHeld) as exc:
        _fail(exc)
    if code:
        raise typer.Exit(code=code)


def _bind(
    infra: InfraConfig, host: str | None, port: int | None, credentials: tuple[str, str] | None
) -> tuple[str, int]:
    bind_host = host or infra.dashboard.host
    bind_port = port or infra.dashboard.port
    if credentials is None and not _is_loopback(bind_host):
        log.warning(
            "the dashboard listens on %s with no password: anyone who can reach port %d can "
            "use it. Set DASHBOARD_USER and DASHBOARD_PASSWORD, or publish the port on "
            "127.0.0.1 only (as docker-compose.yml does)",
            bind_host,
            bind_port,
        )
    return bind_host, bind_port


def _quiet_probes() -> None:
    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, _SkipProbes) for item in access_log.filters):
        access_log.addFilter(_SkipProbes())


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
