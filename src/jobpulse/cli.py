"""jobpulse command-line interface (Typer)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from jobpulse import __version__
from jobpulse.config import (
    DEFAULT_ENV_FILE,
    AppConfig,
    ConfigError,
    Settings,
    config_warnings,
    load_config,
    load_settings,
    require_secrets,
    resolve_config_path,
)
from jobpulse.db import migrate
from jobpulse.db.session import display_url, resolve_database_url

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
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(companies_app, name="companies")


@dataclass(frozen=True)
class CliState:
    config_path: Path
    env_file: Path
    settings: Settings


def _fail(error: object) -> NoReturn:
    typer.echo(f"jobpulse: {error}", err=True)
    raise typer.Exit(code=2)


def _not_built_yet(milestone: str) -> NoReturn:
    _fail(f"this command arrives in {milestone}; see PROGRESS.md")


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


def _database_url(state: CliState) -> str:
    """Config is optional here: without config.yaml, DATABASE_URL or the default applies."""
    config = _load_config(state) if state.config_path.exists() else None
    return resolve_database_url(state.settings, config)


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
    """Validate config.yaml and the secrets your notification channels need."""
    state = _state(ctx)
    config = _load_config(state)
    try:
        require_secrets(config, state.settings, env_file=state.env_file)
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
def poll() -> None:
    """Run one poll cycle over the watchlist."""
    _not_built_yet("M1")


@app.command()
def worker() -> None:
    """Run the scheduler: poll every source on its own interval."""
    _not_built_yet("M1")


@companies_app.command("list")
def companies_list() -> None:
    """List watchlist companies and their baseline state."""
    _not_built_yet("M1")


@app.command()
def web() -> None:
    """Serve the dashboard on 127.0.0.1."""
    _not_built_yet("M3")


@app.command()
def digest() -> None:
    """Send the digest of matches that didn't warrant an instant push."""
    _not_built_yet("M3")
