"""The dashboard application: templates, static files, security middleware and routes."""

from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import jinja2
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException

from jobpulse.config import InfraConfig
from jobpulse.core.timeparse import utcnow
from jobpulse.credentials import SecretStore
from jobpulse.httpclient import HttpClient
from jobpulse.logging_setup import SecretRegistry
from jobpulse.runtime import build_http
from jobpulse.seed import REMOTE_TARGET
from jobpulse.seed import SOURCE as SEED_SOURCE
from jobpulse.sources.ats_detect import AtsDetector
from jobpulse.web import routes
from jobpulse.web.security import (
    BasicAuthMiddleware,
    CsrfCookieMiddleware,
    SecurityHeadersMiddleware,
)
from jobpulse.web.state import WebState
from jobpulse.web.views import FILTERS

HERE = Path(__file__).parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"


def _redactor(registry: SecretRegistry) -> Callable[[Any], Any]:
    """Jinja ``finalize``: every rendered value loses any secret it contains, including those
    saved on Settings after the dashboard started."""

    def finalize(value: Any) -> Any:
        if value is None:
            return ""
        if not isinstance(value, str) or not registry.values():
            return value
        text = registry.redact(str(value))
        return Markup(text) if isinstance(value, Markup) else text

    return finalize


def build_templates(secrets: Iterable[str] | SecretRegistry) -> Jinja2Templates:
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        finalize=_redactor(
            secrets if isinstance(secrets, SecretRegistry) else SecretRegistry(secrets)
        ),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters.update(FILTERS)
    return Jinja2Templates(env=environment)


def create_app(
    *,
    infra: InfraConfig,
    sessions: sessionmaker[Session],
    credentials: tuple[str, str] | None = None,
    store: SecretStore | None = None,
    http_factory: Callable[[], HttpClient] | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> FastAPI:
    """The dashboard. ``credentials`` (user, password) turn on basic auth; ``store`` resolves
    the secrets Settings shows and saves, and its registry's values are masked in every
    rendered value; ``http_factory`` builds the client the watchlist page detects boards
    with and test pushes go through (default: the same client the poller uses)."""
    store = store if store is not None else SecretStore()
    store.registry.add(credentials[1:] if credentials else ())
    state = WebState(
        infra=infra,
        sessions=sessions,
        templates=build_templates(store.registry),
        clock=clock,
        store=store,
        feeds=([("simplify", infra.simplify.name)] if infra.simplify.enabled else [])
        + ([(SEED_SOURCE, REMOTE_TARGET)] if infra.seed.remote else []),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        http = http_factory() if http_factory is not None else build_http(infra)
        state.http = http
        state.detector = AtsDetector(http)
        state._snapshot = None  # the first page sets the User-Agent from the profile
        try:
            yield
        finally:
            await http.aclose()

    app = FastAPI(
        title="jobpulse", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.web = state
    app.include_router(routes.pages)
    app.include_router(routes.actions)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # Starlette's base class, so unknown paths (404) get the same plain answer.
    app.add_exception_handler(HTTPException, _http_error)  # type: ignore[arg-type]
    # Starlette runs the last-added middleware first: headers wrap auth, which wraps CSRF.
    app.add_middleware(CsrfCookieMiddleware)
    if credentials is not None:
        app.add_middleware(BasicAuthMiddleware, username=credentials[0], password=credentials[1])
    app.add_middleware(SecurityHeadersMiddleware)
    return app


async def _http_error(request: Request, exc: HTTPException) -> Response:
    """Plain text for ordinary requests; for HTMX, a message HTMX shows in the page's
    notice area instead of where the result would have gone."""
    detail = str(exc.detail)
    if request.headers.get("hx-request") != "true":
        return PlainTextResponse(detail, status_code=exc.status_code, headers=exc.headers)
    return HTMLResponse(
        f'<p class="notice error" role="alert">{escape(detail)}</p>',
        status_code=exc.status_code,
        headers={**(exc.headers or {}), "HX-Retarget": "#notice", "HX-Reswap": "innerHTML"},
    )
