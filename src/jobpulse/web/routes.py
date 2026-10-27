"""Dashboard pages (GET) and actions (POST, each behind the CSRF check).

Every action works as a plain form post (it redirects back) and through HTMX (it answers with
the HTML fragment that replaces the one you clicked in).
"""

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.types import DecoratedCallable
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpulse.config import SLUG_PATTERN, AppConfig, clean_keywords
from jobpulse.core.atsurls import (
    AtsRef,
    detect_ats_url,
    detect_workday_url,
    gh_jid,
    normalize_workday_token,
    workday_board,
)
from jobpulse.core.places import parse_place_key
from jobpulse.db import dashboard, repo
from jobpulse.db.dashboard import (
    PUSHED_CHOICES,
    PUSHED_WINDOWS,
    STATUSES,
    PostingQuery,
    SortOrder,
)
from jobpulse.db.session import session_scope
from jobpulse.db.tables import Company, PostingRow
from jobpulse.runtime import KNOWN_SOURCES
from jobpulse.sources.ats_detect import Validation, is_fetchable_url
from jobpulse.sources.workday import PAGE_LIMIT
from jobpulse.web.security import verify_csrf
from jobpulse.web.views import link

pages = APIRouter()
actions = APIRouter(dependencies=[Depends(verify_csrf)])


def page(path: str, **options: Any) -> Callable[[DecoratedCallable], DecoratedCallable]:
    """A GET route that answers HEAD too (uptime checks and link previewers send HEAD;
    FastAPI routes, unlike plain Starlette ones, don't add it). The server drops the body."""
    return pages.api_route(path, methods=["GET", "HEAD"], **options)


VIEWS = {status.lower(): status for status in STATUSES}
SORTS: dict[str, SortOrder] = {"score": "score", "fresh": "fresh", "pushed": "pushed"}
ADDABLE_ATS = ("greenhouse", "lever", "ashby", "workday")
ATS_NAMES = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workday": "Workday"}
MAX_PRIORITY = 1000
_SLUG = re.compile(SLUG_PATTERN)


def _state(request: Request) -> Any:
    return request.app.state.web


def _htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _blank(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def parse_query(params: Mapping[str, str]) -> PostingQuery:
    """Filters from the query string. Blank or unreadable values are ignored, not errors."""
    status = VIEWS.get((params.get("view") or "").strip().lower(), "New")
    source = _blank(params.get("source"))
    remote = {"yes": True, "no": False}.get((params.get("remote") or "").strip().lower())
    min_score: float | None = None
    try:
        number = float(params.get("min_score") or "")
        if math.isfinite(number):
            min_score = max(0.0, number)
    except ValueError:
        pass
    try:
        page = max(1, int(params.get("page") or "1"))
    except ValueError:
        page = 1
    pushed = (params.get("pushed") or "").strip()
    return PostingQuery(
        status=status,
        source=source if source in KNOWN_SOURCES else None,
        company=_blank(params.get("company")),
        remote=remote,
        min_score=min_score,
        text=_blank(params.get("q")),
        open_only=_checked(params.get("open")),
        place=parse_place_key(params.get("place") or ""),
        pushed=pushed if pushed in PUSHED_WINDOWS else None,
        min_pay=_thousands(params.get("pay")),
        pay_listed=_checked(params.get("pay_listed")),
        sort=SORTS.get(params.get("sort") or "", "score"),
        page=page,
    )


def _checked(value: str | None) -> bool:
    return (value or "") in ("1", "on", "true", "yes")


def _thousands(value: str | None) -> int | None:
    """A pay floor in thousands of dollars: a positive whole number, else no floor."""
    try:
        number = int((value or "").strip())
    except ValueError:
        return None
    return number if number > 0 else None


def safe_return_to(value: str | None, default: str = "/") -> str:
    """A same-site path to go back to after a plain form POST; anything else is ``default``."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return default
    if "\\" in value or any(ord(char) < 32 for char in value):
        return default
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return default
    return value


def render(
    request: Request,
    template: str,
    context: dict[str, Any],
    *,
    session: Session | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """A page or fragment. With ``session``, the navigation counts are added (full pages)."""
    state = _state(request)
    values: dict[str, Any] = {
        "now": state.clock(),
        "short_labels": state.short_labels,
        "zone": state.zone,
        "csrf_token": request.state.csrf_token,
        "statuses": STATUSES,
        "view": None,
        "priority_cap": state.config.scoring.priority_cap,
        **context,
    }
    if session is not None:
        hidden = dashboard.hidden_companies(session, state.blocklist)
        values["counts"] = dashboard.view_counts(session, hidden=hidden)
        values["incident_count"] = len(dashboard.open_incidents(session))
    return state.templates.TemplateResponse(request, template, values, status_code=status_code)


# --- postings --------------------------------------------------------------------------------


@page("/", response_class=HTMLResponse)
def postings_page(request: Request) -> HTMLResponse:
    state = _state(request)
    query = parse_query(request.query_params)
    with state.sessions() as session:
        hidden = dashboard.hidden_companies(session, state.blocklist)
        page = dashboard.list_postings(session, query, now=state.clock(), hidden=hidden)
        return render(
            request,
            "postings.html",
            {
                "query": query,
                "page": page,
                "view": query.status,
                "sources": KNOWN_SOURCES,
                "pushed_choices": PUSHED_CHOICES,
                "link": link,
                "return_to": str(request.url.path)
                + (f"?{request.url.query}" if request.url.query else ""),
            },
            session=session,
        )


@actions.post("/postings/{posting_id}/status")
def change_status(
    request: Request,
    posting_id: int,
    status: Annotated[str, Form()],
    view: Annotated[str, Form()] = "",
    return_to: Annotated[str, Form()] = "",
) -> Response:
    if status not in STATUSES:
        raise HTTPException(status_code=400, detail=f"unknown status {status!r}")
    state = _state(request)
    current_view = VIEWS.get(view.strip().lower())
    with session_scope(state.sessions) as session:
        previous = repo.set_status(session, posting_id, status)
        if previous is None:
            raise HTTPException(status_code=404, detail="no such posting")
        if not _htmx(request):
            return RedirectResponse(safe_return_to(return_to), status_code=303)
        row = session.get(PostingRow, posting_id)
        hidden = dashboard.hidden_companies(session, state.blocklist)
        counts = dashboard.view_counts(session, hidden=hidden)
        template = "_posting.html" if current_view in (None, status) else "_moved.html"
        return render(
            request,
            template,
            {
                "row": row,
                "view": current_view or status,
                "previous": previous,
                "counts": counts,
                "oob_counts": True,
                "return_to": safe_return_to(return_to),
            },
        )


# --- source health ---------------------------------------------------------------------------


@page("/health", response_class=HTMLResponse)
def health_page(request: Request) -> HTMLResponse:
    state = _state(request)
    with state.sessions() as session:
        targets = dashboard.known_targets(session, blocklist=state.blocklist, feeds=state.feeds)
        return render(
            request,
            "health.html",
            {
                "view": "health",
                "targets": dashboard.health_overview(session, now=state.clock(), targets=targets),
                "incidents": dashboard.open_incidents(session),
                "failures_needed": state.config.health.consecutive_failures,
                "quiet_hours": f"{state.config.health.zero_hours:g}",
            },
            session=session,
        )


@page("/healthz", response_class=PlainTextResponse)
def healthz(request: Request) -> str:
    """Liveness for Docker: the app answers and the database does too. No auth, no data."""
    with _state(request).sessions() as session:
        session.execute(select(1))
    return "ok"


# --- watchlist -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Detection:
    """What the paste box found: a board ready to add, or a message saying why not."""

    url: str
    message: str | None = None
    tone: Literal["error", "info", "success"] = "error"
    ref: AtsRef | None = None
    validation: Validation | None = None
    name: str = ""
    warning: str | None = None

    @property
    def addable(self) -> bool:
        return self.ref is not None and self.validation is not None and self.message is None

    @property
    def ats_name(self) -> str:
        return ATS_NAMES.get(self.ref.ats, self.ref.ats) if self.ref else ""


def _companies(request: Request, session: Session) -> list[dict[str, Any]]:
    blocklist = _state(request).blocklist
    return [
        {
            "company": company,
            "blocked": blocklist.blocks_board(company.ats, company.token)
            or blocklist.blocks_company(company.name),
        }
        for company in repo.list_companies(session)
    ]


def _watchlist_page(
    request: Request, *, detection: Detection | None = None, status_code: int = 200
) -> HTMLResponse:
    with _state(request).sessions() as session:
        return render(
            request,
            "watchlist.html",
            {
                "view": "watchlist",
                "companies": _companies(request, session),
                "detection": detection,
                "added": _blank(request.query_params.get("added")),
            },
            session=session,
            status_code=status_code,
        )


@page("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request) -> HTMLResponse:
    return _watchlist_page(request)


async def _detect(request: Request, url: str) -> Detection:
    """URL first, then the page once; then one validation call for a new, unblocked board."""
    state = _state(request)
    if not url:
        return Detection(url, "Paste the URL of a careers page, job board or job posting.")
    ref = detect_ats_url(url) or detect_workday_url(url)
    if ref is None:
        if not is_fetchable_url(url):
            return Detection(url, "That isn't a public http(s) URL, so it wasn't fetched.")
        refs = await state.detector.refs_in_page(url)
        if refs is None:
            return Detection(url, "Couldn't read that page: it didn't answer with HTTP 200.")
        if not refs:
            return Detection(
                url,
                "No Greenhouse, Lever or Ashby board, and no Workday site, found on that page. "
                "iCIMS and other systems aren't supported.",
            )
        ref = next((r for r in refs if r.ats == "greenhouse"), refs[0]) if gh_jid(url) else refs[0]
    board = f"{ref.ats}/{ref.token}"
    with state.sessions() as session:
        known = (ref.ats, ref.token.lower()) in repo.known_companies(session)
    if known:
        return Detection(url, f"{board} is already on your watchlist.", "info", ref)
    if state.blocklist.blocks_board(ref.ats, ref.token):
        return Detection(url, f"{board} is on your blocklist (blocklist.boards in config.yaml).")
    validation = await state.detector.validate(ref)
    if not validation.ok:
        return Detection(url, f"{board} couldn't be validated: {validation.error}", ref=ref)
    name = validation.name or _default_name(ref)
    if state.blocklist.blocks_company(name):
        return Detection(
            url, f"{name} is on your blocklist (blocklist.companies in config.yaml).", ref=ref
        )
    return Detection(
        url,
        ref=validation.ref,
        validation=validation,
        name=name,
        tone="success",
        warning=_size_warning(state.config, ref, validation.job_count),
    )


def _default_name(ref: AtsRef) -> str:
    """What the name field starts with when the board doesn't say: a Workday tenant, else
    the token."""
    board = workday_board(ref.token) if ref.ats == "workday" else None
    return board.tenant if board is not None else ref.token


def _size_warning(config: AppConfig, ref: AtsRef, job_count: int) -> str | None:
    if ref.ats == "workday":
        cap = config.workday.max_pages * PAGE_LIMIT
        if job_count <= cap:
            return None
        return (
            f"{job_count} open jobs is more than one poll reads without keywords ({cap}: "
            f"workday.max_pages pages of {PAGE_LIMIT}): add keywords, and each one is searched "
            "instead."
        )
    limit = config.discovery.max_board_jobs
    if limit is None or job_count <= limit:
        return None
    return (
        f"{job_count} open jobs is more than discovery adds on its own ({limit}); every poll "
        "downloads all of them."
    )


@actions.post("/watchlist/detect", response_class=HTMLResponse)
async def detect_board(request: Request, url: Annotated[str, Form()] = "") -> HTMLResponse:
    detection = await _detect(request, url.strip())
    if not _htmx(request):
        return _watchlist_page(request, detection=detection)
    return render(request, "_detect.html", {"detection": detection})


def _read_priority(value: str) -> int:
    try:
        priority = int(value.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="priority must be a whole number") from None
    if not 0 <= priority <= MAX_PRIORITY:
        raise HTTPException(status_code=400, detail=f"priority must be 0 to {MAX_PRIORITY}")
    return priority


@actions.post("/watchlist/add", response_class=HTMLResponse)
def add_board(
    request: Request,
    ats: Annotated[str, Form()],
    token: Annotated[str, Form()],
    region: Annotated[str, Form()] = "us",
    name: Annotated[str, Form()] = "",
    priority: Annotated[str, Form()] = "0",
    url: Annotated[str, Form()] = "",
    keywords: Annotated[str, Form()] = "",
) -> Response:
    state = _state(request)
    token, name = token.strip(), " ".join(name.split())
    params: dict[str, Any] | None = None
    if ats not in ADDABLE_ATS:
        raise HTTPException(
            status_code=400, detail="ats must be greenhouse, lever, ashby or workday"
        )
    if ats == "workday":
        token = normalize_workday_token(token) or ""
        if not token:
            raise HTTPException(
                status_code=400, detail="that isn't a Workday board (tenant.wdN/site)"
            )
        try:
            words = clean_keywords(keywords.split(","))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        params = {"keywords": words} if words else None
    elif not _SLUG.fullmatch(token) or len(token) > 200:
        raise HTTPException(status_code=400, detail=f"{token!r} isn't a board token")
    if region not in ("us", "eu"):
        raise HTTPException(status_code=400, detail="region must be us or eu")
    if not name or len(name) > 200:
        raise HTTPException(status_code=400, detail="the company needs a name")
    value = _read_priority(priority)
    if state.blocklist.blocks_board(ats, token) or state.blocklist.blocks_company(name):
        raise HTTPException(
            status_code=400, detail=f"{ats}/{token} ({name}) is on your blocklist in config.yaml"
        )
    source_url = url.strip() if url.strip().startswith(("https://", "http://")) else None
    with session_scope(state.sessions) as session:
        added = repo.add_company(
            session,
            ats=ats,
            token=token,
            region=region,
            name=name,
            source="dashboard",
            priority=value,
            discovered_from=source_url,
            now=state.clock(),
            params=params,
        )
        companies = _companies(request, session) if added else None
    board = f"{ats}/{token}"
    if not _htmx(request):
        return RedirectResponse(f"/watchlist?added={board}" if added else "/watchlist", 303)
    searching = (
        f", searching for {', '.join(params['keywords'])}"
        if params and params.get("keywords")
        else ""
    )
    detection = (
        Detection(
            source_url or "",
            f"Added {name} ({board}) with priority {value}{searching}. Its first poll stores the "
            "jobs it has now without notifying you; new ones after that reach you as usual.",
            "success",
        )
        if added
        else Detection(source_url or "", f"{board} is already on your watchlist.", "info")
    )
    return render(request, "_detect.html", {"detection": detection, "companies": companies})


def _editable_company(session: Session, company_id: int) -> Company:
    company = session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="no such company")
    if company.source_of_discovery == "config":
        raise HTTPException(
            status_code=400,
            detail=f"{company.name} comes from config.yaml; change it there (each poll applies "
            "config.yaml again)",
        )
    return company


def _company_response(request: Request, session: Session, company_id: int) -> Response:
    if not _htmx(request):
        return RedirectResponse("/watchlist", status_code=303)
    entry = next(item for item in _companies(request, session) if item["company"].id == company_id)
    return render(request, "_company.html", entry)


@actions.post("/watchlist/{company_id}/priority", response_class=HTMLResponse)
def set_priority(
    request: Request, company_id: int, priority: Annotated[str, Form()] = ""
) -> Response:
    value = _read_priority(priority)
    with session_scope(_state(request).sessions) as session:
        _editable_company(session, company_id)
        repo.update_company(session, company_id, priority=value)
        return _company_response(request, session, company_id)


@actions.post("/watchlist/{company_id}/active", response_class=HTMLResponse)
def set_active(request: Request, company_id: int, active: Annotated[str, Form()] = "") -> Response:
    with session_scope(_state(request).sessions) as session:
        _editable_company(session, company_id)
        repo.update_company(session, company_id, active=active.strip() in ("1", "true", "on"))
        return _company_response(request, session, company_id)
