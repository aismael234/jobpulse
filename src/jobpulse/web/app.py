"""The dashboard application: templates, static files, security middleware and routes."""

from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import jinja2
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException

from jobpulse.config import AppConfig, company_blocklist, short_labels
from jobpulse.core.filters import Blocklist
from jobpulse.core.timeparse import utcnow
from jobpulse.httpclient import HttpClient
from jobpulse.logging_setup import REDACTED
from jobpulse.runtime import build_http
from jobpulse.sources.ats_detect import AtsDetector
from jobpulse.web import routes
from jobpulse.web.security import (
    BasicAuthMiddleware,
    CsrfCookieMiddleware,
    SecurityHeadersMiddleware,
)
from jobpulse.web.views import FILTERS

HERE = Path(__file__).parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"
MIN_SECRET_LENGTH = 4


@dataclass
class WebState:
    """Everything the routes need, kept on ``app.state.web``."""

    config: AppConfig
    sessions: sessionmaker[Session]
    templates: Jinja2Templates
    clock: Callable[[], datetime]
    blocklist: Blocklist
    short_labels: dict[str, str]
    zone: ZoneInfo
    detector: AtsDetector | None = None
    feeds: list[tuple[str, str]] = field(default_factory=list)


def _redactor(secrets: Iterable[str]) -> Callable[[Any], Any]:
    """Jinja ``finalize``: every rendered value loses any secret it contains."""
    values = sorted(
        {secret for secret in secrets if len(secret) >= MIN_SECRET_LENGTH}, key=len, reverse=True
    )

    def finalize(value: Any) -> Any:
        if value is None:
            return ""
        if not values or not isinstance(value, str):
            return value
        text = str(value)
        for secret in values:
            text = text.replace(secret, REDACTED)
        return Markup(text) if isinstance(value, Markup) else text

    return finalize


def build_templates(secrets: Iterable[str]) -> Jinja2Templates:
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        finalize=_redactor(secrets),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters.update(FILTERS)
    return Jinja2Templates(env=environment)


def create_app(
    *,
    config: AppConfig,
    sessions: sessionmaker[Session],
    credentials: tuple[str, str] | None = None,
    secrets: Iterable[str] = (),
    http_factory: Callable[[], HttpClient] | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> FastAPI:
    """The dashboard. ``credentials`` (user, password) turn on basic auth; ``secrets`` are
    masked in every rendered value; ``http_factory`` builds the client the watchlist page
    detects boards with (default: the same client the poller uses)."""
    state = WebState(
        config=config,
        sessions=sessions,
        templates=build_templates([*secrets, *(credentials[1:] if credentials else ())]),
        clock=clock,
        blocklist=company_blocklist(config),
        short_labels=short_labels(config),
        zone=ZoneInfo(config.timezone),
        feeds=[("simplify", config.simplify.name)] if config.simplify.enabled else [],
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        http = http_factory() if http_factory is not None else build_http(config)
        state.detector = AtsDetector(http)
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
