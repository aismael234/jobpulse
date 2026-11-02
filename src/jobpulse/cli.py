"""jobpulse command-line interface (Typer)."""

import asyncio
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from sqlalchemy.orm import Session, sessionmaker

from jobpulse import __version__
from jobpulse.board_import import ImportReport, import_boards, read_links, render_import
from jobpulse.config import (
    AppConfig,
    ConfigError,
    InfraConfig,
    Profile,
    ProfileError,
    clean_keywords,
    company_blocklist,
    compose,
    config_warnings,
    default_models_dir,
    filter_rules,
    parse_profile_yaml,
    profile_yaml,
    require_secrets,
    scoring_weights,
)
from jobpulse.core.similarity import resume_lines
from jobpulse.core.timeparse import utcnow
from jobpulse.credentials import SecretStore, clear_password
from jobpulse.db import migrate, repo
from jobpulse.db import profile as profile_db
from jobpulse.db import resume as resume_db
from jobpulse.db.lease import LeaseHeld
from jobpulse.db.session import DEFAULT_DATA_DIR, database_url, display_url, session_scope
from jobpulse.devtools.fixtures import FixtureScraper, FixtureTransport, fixture_infra
from jobpulse.embeddings.base import EmbedderUnavailable
from jobpulse.embeddings.fastembed import DEFAULT_MODEL, download_model
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
    build_embedder,
    build_http,
    open_database,
    poll_once,
    render_report,
    send_test_notification,
    stored_infra,
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

log = logging.getLogger("jobpulse")

app = typer.Typer(
    name="jobpulse",
    help="Personal job-posting watcher: finds fresh postings and notifies you. It never applies.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
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
password_app = typer.Typer(
    help="The dashboard password (Settings → Security).", no_args_is_help=True
)
app.add_typer(db_app, name="db")
app.add_typer(companies_app, name="companies")
companies_app.add_typer(seed_app, name="seed")
app.add_typer(notify_app, name="notify")
app.add_typer(embeddings_app, name="embeddings")
app.add_typer(resume_app, name="resume")
app.add_typer(profile_app, name="profile")
app.add_typer(password_app, name="password")


@dataclass(frozen=True)
class CliState:
    data_dir: Path
    store: SecretStore
    log_level: str = "INFO"

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


def _database_url(state: CliState) -> str:
    return database_url(state.data_dir)


def _sessions(state: CliState) -> sessionmaker[Session]:
    try:
        return open_database(_database_url(state))
    except DatabaseNotReady as exc:
        _fail(exc)


def _infra(state: CliState, sessions: sessionmaker[Session]) -> InfraConfig:
    """How the worker runs: the fixed defaults with the profile's Sources & schedule."""
    return stored_infra(sessions, data_dir=state.data_dir)


def _app_config(
    state: CliState, infra: InfraConfig, sessions: sessionmaker[Session], *, required: bool = True
) -> AppConfig:
    """How the worker runs composed with the stored profile. ``required``: a command that polls
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
        require_secrets(config, secrets.values, llm=llm)
    except ConfigError as exc:
        _fail(exc)


def _start_logging(state: CliState, config: AppConfig | None = None) -> None:
    configure_logging(state.log_level, secrets=state.registry)
    for warning in config_warnings(config) if config is not None else ():
        log.warning("config: %s", warning)


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"jobpulse {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    data_dir: Annotated[
        Path,
        typer.Option(
            "--data-dir",
            help="Folder for the database and its backups (default: ./data). Everything else "
            "is set on the dashboard.",
            file_okay=False,
        ),
    ] = DEFAULT_DATA_DIR,
    log_level: Annotated[
        str, typer.Option("--log-level", help="DEBUG, INFO, WARNING or ERROR.")
    ] = "INFO",
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_show_version, is_eager=True, help="Show the version and exit."
        ),
    ] = False,
) -> None:
    ctx.obj = CliState(data_dir=data_dir, store=SecretStore(), log_level=log_level.upper())


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
    sessions = _sessions(state)
    infra = _infra(state, sessions)
    if fixtures is not None:
        infra = fixture_infra(fixtures, infra)
    unknown = sorted(set(source or ()) - set(KNOWN_SOURCES))
    if unknown:
        _fail(f"unknown source {', '.join(unknown)}; choose from {', '.join(KNOWN_SOURCES)}")
    config = _app_config(state, infra, sessions)
    if not dry_run:
        _require_secrets(state, config, sessions, llm=True)
    _start_logging(state, config)
    try:
        report = asyncio.run(
            poll_once(
                infra=infra,
                store=state.store,
                database_url=_database_url(state),
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


@companies_app.command("list")
def companies_list(ctx: typer.Context) -> None:
    """List watchlist companies with their baseline state and open postings."""
    state = _state(ctx)
    sessions = _sessions(state)
    infra = _infra(state, sessions)
    config = _app_config(state, infra, sessions, required=False)
    with session_scope(sessions) as session:
        companies = repo.list_companies(session)
        blocklist = repo.blocklist(  # with the dashboard's
            session, company_blocklist(config), agencies=config.hide_agencies
        )
    if not companies:
        typer.echo("no companies yet; finish the setup wizard, or add boards on the Watchlist page")
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
    sessions = _sessions(state)
    infra = _infra(state, sessions)
    try:
        keywords = clean_keywords(workday_keywords.split(","))
    except ValueError as exc:
        _fail(exc)
    try:
        links = read_links(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        _fail(f"couldn't read {file}: {exc}")
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
    sessions = _sessions(state)
    infra = _infra(state, sessions)
    seed = _read_seed(file)
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
    sessions = _sessions(state)
    infra = _infra(state, sessions)
    try:
        extra = [parse_extra(value) for value in add or ()]
    except ValueError as exc:
        _fail(exc)
    previous = _read_seed(out if out is not None and out.exists() else None)
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
    sessions = _sessions(state)
    seed = _read_seed(None)
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
    sessions = _sessions(state)
    infra = _infra(state, sessions)
    config = _app_config(state, infra, sessions)
    _require_secrets(state, config, sessions)
    _start_logging(state, config)
    channels = asyncio.run(
        send_test_notification(store=state.store, database_url=_database_url(state))
    )
    if not channels:
        _fail("no notification channels configured (notify.channels, on Settings)")
    typer.echo(f"test notification sent via {', '.join(channels)}")


@embeddings_app.command("download")
def embeddings_download(
    cache_dir: Annotated[
        Path | None,
        typer.Option(
            "--cache-dir",
            help="Where to keep the model (default: JOBPULSE_MODELS_DIR, data/models).",
        ),
    ] = None,
) -> None:
    """Download the embedding model from Hugging Face (the only command that downloads it)."""
    cache_dir = cache_dir or default_models_dir()
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
    sessions = _sessions(state)
    infra = _infra(state, sessions)
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
    sessions = _sessions(state)
    infra = _infra(state, sessions)
    embedder = build_embedder(infra)
    if embedder is None:
        _fail("resume similarity is off (embeddings.enabled is false, or fastembed is missing)")
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
        Path,
        typer.Argument(help="A profile written by `jobpulse profile export`.", dir_okay=False),
    ],
    replace: Annotated[
        bool, typer.Option("--replace", help="Overwrite a different profile already stored.")
    ] = False,
) -> None:
    """Restore a profile saved with `jobpulse profile export` (what Settings → Advanced
    shows). It counts as onboarded."""
    state = _state(ctx)
    try:
        profile = parse_profile_yaml(file.read_text(encoding="utf-8"))
    except OSError as exc:
        _fail(f"couldn't read {file}: {exc}")
    except ProfileError as exc:
        _fail(f"{file}: {exc}")
    with session_scope(_sessions(state)) as session:
        stored = profile_db.get(session)
        if stored.profile == profile and stored.onboarded:
            typer.echo(f"profile: unchanged (revision {stored.revision})")
            return
        if stored.revision and not replace:
            _fail(
                f"a different profile is already stored (revision {stored.revision}); pass "
                "--replace to overwrite it"
            )
        revision = profile_db.save(session, profile, now=utcnow())
    typer.echo(f"profile: imported from {file} (revision {revision})")


@profile_app.command("export")
def profile_export(ctx: typer.Context) -> None:
    """Print the stored profile as YAML (what Settings shows under Profile)."""
    with _sessions(_state(ctx))() as session:
        stored = profile_db.get(session)
    if stored.profile is None:
        _fail(stored.problem or "no profile stored; finish the setup wizard on the dashboard")
    typer.echo(profile_yaml(stored.profile), nl=False)


@profile_app.command("show")
def profile_show(ctx: typer.Context) -> None:
    """Summarize the stored profile."""
    with _sessions(_state(ctx))() as session:
        stored = profile_db.get(session)
    if stored.revision == 0:
        typer.echo("no profile stored; finish the setup wizard on the dashboard")
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
def up(
    ctx: typer.Context,
    host: Annotated[
        str, typer.Option("--host", help="Address to listen on (0.0.0.0 in Docker).")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port.", min=1, max=65535)] = 8000,
    no_migrate: Annotated[
        bool,
        typer.Option("--no-migrate", help="Don't back up and upgrade the database first."),
    ] = False,
) -> None:
    """Run jobpulse: back up and upgrade the database, then serve the dashboard with the
    scheduler alongside. A new install opens the setup wizard; everything is set there and
    on Settings."""
    state = _state(ctx)
    _start_logging(state)
    _quiet_probes()
    try:
        code = run_app(
            store=state.store,
            database_url=_database_url(state),
            host=host,
            port=port,
            data_dir=state.data_dir,
            migrate_first=not no_migrate,
        )
    except (DatabaseNotReady, LeaseHeld) as exc:
        _fail(exc)
    if code:
        raise typer.Exit(code=code)


@password_app.command("clear")
def password_clear(ctx: typer.Context) -> None:
    """Remove the dashboard password, if you can't sign in (set a new one on Settings)."""
    with session_scope(_sessions(_state(ctx))) as session:
        removed = clear_password(session)
    typer.echo("dashboard password removed" if removed else "the dashboard has no password")


def _quiet_probes() -> None:
    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, _SkipProbes) for item in access_log.filters):
        access_log.addFilter(_SkipProbes())


class _SkipProbes(logging.Filter):
    """Docker's health check calls /healthz every 30 s; those lines are only noise."""

    def filter(self, record: logging.LogRecord) -> bool:
        return " /healthz " not in record.getMessage()
