"""The dashboard application: templates, static files, security middleware and routes."""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

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
from jobpulse.credentials import SecretStore, password_hash
from jobpulse.httpclient import HttpClient
from jobpulse.logging_setup import SecretRegistry
from jobpulse.runtime import build_http
from jobpulse.sources.ats_detect import AtsDetector
from jobpulse.web import onboarding, routes
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


class Background(Protocol):
    """Something the dashboard runs alongside itself: ``jobpulse up``'s worker thread."""

    def start(self) -> None: ...

    def stop(self) -> None: ...


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
    sessions: sessionmaker[Session],
    infra: InfraConfig | None = None,
    store: SecretStore | None = None,
    http_factory: Callable[[], HttpClient] | None = None,
    clock: Callable[[], datetime] = utcnow,
    worker: Background | None = None,
) -> FastAPI:
    """The dashboard. ``infra`` is the base the profile's Sources & schedule settings apply
    to (default: the fixed defaults); ``store`` resolves the secrets Settings shows and
    saves, and its registry's values are masked in every rendered value; basic auth is on
    while a dashboard password is saved (Settings → Security); ``http_factory`` builds the client the watchlist page detects boards
    with and test pushes go through (default: the same client the poller uses);
    ``worker`` starts with the app and stops with it (``jobpulse up``)."""
    store = store if store is not None else SecretStore()
    state = WebState(
        infra=infra or InfraConfig(),
        sessions=sessions,
        templates=build_templates(store.registry),
        clock=clock,
        store=store,
    )

    def stored_password_hash() -> str | None:
        with sessions() as session:
            return password_hash(session)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        http = http_factory() if http_factory is not None else build_http(state.infra)
        state.http = http
        state.detector = AtsDetector(http)
        state._snapshot = None  # the first page sets the User-Agent from the profile
        if worker is not None:
            worker.start()
        try:
            yield
        finally:
            if worker is not None:
                await asyncio.to_thread(worker.stop)
            for task in list(state.tasks):
                task.cancel()
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
    # Innermost: the wizard redirect runs after auth and once the CSRF cookie is set.
    app.add_middleware(onboarding.OnboardingRedirect)
    app.add_middleware(CsrfCookieMiddleware)
    app.add_middleware(BasicAuthMiddleware, password_hash=stored_password_hash)
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
