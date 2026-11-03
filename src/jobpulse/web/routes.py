"""Dashboard pages (GET) and actions (POST, each behind the CSRF check).

Every action works as a plain form post (it redirects back) and through HTMX (it answers with
the HTML fragment that replaces the one you clicked in).
"""

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.types import DecoratedCallable
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpulse.config import (
    SLUG_PATTERN,
    FieldError,
    InfraConfig,
    ProfileError,
    SourceSettings,
    clean_keywords,
    describe_errors,
    filter_rules,
    parse_profile_yaml,
    profile_yaml,
    scoring_weights,
    split,
)
from jobpulse.core.atsurls import (
    AtsRef,
    default_board_name,
    detect_ats_url,
    detect_workday_url,
    gh_jid,
    normalize_workday_token,
)
from jobpulse.core.dedupe import normalize_company
from jobpulse.core.filters import Blocklist
from jobpulse.core.palette import SWATCHES
from jobpulse.core.similarity import resume_lines
from jobpulse.credentials import (
    MIN_PASSWORD_CHARS,
    SECRET_LABELS,
    SecretError,
    clear_password,
    password_hash,
    set_password,
)
from jobpulse.db import dashboard, repo, saved_filters
from jobpulse.db import profile as profile_db
from jobpulse.db import resume as resume_db
from jobpulse.db.dashboard import (
    CONTRACT_CHOICES,
    EXPERIENCE_CHOICES,
    MAX_APPLICANTS_CHOICES,
    MOVED_SORT_LABELS,
    PUSHED_CHOICES,
    STATUSES,
    PostingQuery,
)
from jobpulse.db.profile import StaleProfile
from jobpulse.db.query_params import VIEWS, filter_params, filter_summary, parse_query
from jobpulse.db.saved_filters import Saved, SavedFilterError
from jobpulse.db.session import session_scope
from jobpulse.db.tables import Company, PostingRow, SavedFilter
from jobpulse.notify.base import NotifierError
from jobpulse.notify.live import NO_CHANNEL, LiveNotifier, send_test
from jobpulse.onboarding import note_custom
from jobpulse.resume_files import MAX_BYTES as MAX_RESUME_BYTES
from jobpulse.resume_files import ResumeFileError, read_resume_bytes
from jobpulse.resume_scoring import restore_stack
from jobpulse.runtime import KNOWN_SOURCES
from jobpulse.sources.ats_detect import Validation, is_fetchable_url
from jobpulse.sources.workday import PAGE_LIMIT
from jobpulse.web.security import verify_csrf
from jobpulse.web.state import Snapshot
from jobpulse.web.views import company_key, export_url, link, postings_csv

pages = APIRouter()
actions = APIRouter(dependencies=[Depends(verify_csrf)])


def page(path: str, **options: Any) -> Callable[[DecoratedCallable], DecoratedCallable]:
    """A GET route that answers HEAD too (uptime checks and link previewers send HEAD;
    FastAPI routes, unlike plain Starlette ones, don't add it). The server drops the body."""
    return pages.api_route(path, methods=["GET", "HEAD"], **options)


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
    snapshot = state.current()
    values: dict[str, Any] = {
        "now": state.clock(),
        "short_labels": snapshot.short_labels,
        "zone": snapshot.zone,
        "csrf_token": request.state.csrf_token,
        "statuses": STATUSES,
        "view": None,
        "priority_cap": snapshot.config.scoring.priority_cap,
        "profile_notice": _profile_notice(snapshot),
        **context,
    }
    if session is not None:
        values["counts"] = _counts(request, session)
        values["incident_count"] = len(dashboard.open_incidents(session))
    return state.templates.TemplateResponse(request, template, values, status_code=status_code)


def _profile_notice(snapshot: Snapshot) -> str | None:
    """The banner every page shows while the worker can't use the profile."""
    if snapshot.problem is not None:
        return f"{snapshot.problem}. The last good profile is in use; fix it on Settings."
    if not snapshot.onboarded:
        return (
            "No profile yet, so the worker waits: answer the setup questions (/setup), or "
            "save one on Settings (Advanced)."
        )
    return None


def _blocklist(request: Request, session: Session) -> Blocklist:
    """The profile's blocklist with the companies blocked from the dashboard."""
    snapshot = _state(request).current()
    return repo.blocklist(session, snapshot.blocklist, agencies=snapshot.config.hide_agencies)


def _hidden(request: Request, session: Session) -> frozenset[str]:
    return dashboard.hidden_companies(session, _blocklist(request, session))


def _counts(request: Request, session: Session) -> dict[str, int]:
    return dashboard.view_counts(session, hidden=_hidden(request, session))


# --- postings --------------------------------------------------------------------------------


@page("/", response_class=HTMLResponse)
def postings_page(request: Request) -> HTMLResponse:
    state = _state(request)
    query = parse_query(request.query_params)
    now = state.clock()
    with state.sessions() as session:
        hidden = _hidden(request, session)
        page = dashboard.list_postings(session, query, now=now, hidden=hidden)
        empty_new = query.status == "New" and not page.rows and not query.filtered
        progress = dashboard.watch_progress(session) if empty_new else None
        cadence = timedelta(minutes=state.current().config.schedules.watchlist.every_minutes)
        return render(
            request,
            "postings.html",
            {
                "query": query,
                "page": page,
                "view": query.status,
                "sources": KNOWN_SOURCES,
                "pushed_choices": PUSHED_CHOICES,
                "experience_choices": EXPERIENCE_CHOICES,
                "max_applicants_choices": MAX_APPLICANTS_CHOICES,
                "contract_choices": CONTRACT_CHOICES,
                "reposted_box": query.status == "New",
                "progress": progress,
                "next_check": progress.last_check + cadence
                if progress is not None and progress.last_check is not None
                else None,
                **_saved_context(
                    session,
                    query,
                    page.rows,
                    now=now,
                    hidden=hidden,
                    editing=request.query_params.get("editing", ""),
                ),
                "moved_sort": MOVED_SORT_LABELS.get(query.status),
                "bulk_views": BULK_VIEWS,
                "link": link,
                "export_url": export_url(query),
                "filters": link(query, page=1),
                "as_of": now.isoformat(),
                "return_to": str(request.url.path)
                + (f"?{request.url.query}" if request.url.query else ""),
            },
            session=session,
        )


@dataclass(frozen=True, slots=True)
class SavedButton:
    saved: Saved
    href: str
    count: int  # new matches since it was last opened
    current: bool  # its filters are the ones on the page


def _saved_context(
    session: Session,
    query: PostingQuery,
    rows: list[PostingRow],
    *,
    now: datetime,
    hidden: frozenset[str],
    editing: str = "",
) -> dict[str, Any]:
    """The saved-filter buttons above the filters, the bullets on this page's cards, and the
    "Save these filters" form (``editing``: a saved filter's id, to update it instead)."""
    every = saved_filters.list_filters(session)
    filters = [saved for saved in every if saved.query is not None]
    counts = saved_filters.new_match_counts(session, filters, now=now, hidden=hidden)
    shown = filter_params(query)
    buttons = [
        SavedButton(
            saved,
            link(replace(saved.query, status="New", sort="score", page=1)),
            counts.get(saved.row.id, 0),
            query.status == "New" and shown == saved.row.params,
        )
        for saved in filters
        if saved.query is not None
    ]
    bullets = saved_filters.fitting(
        session, filters, [row.id for row in rows], now=now, status=query.status
    )
    plain = replace(query, status="New", sort="score", page=1, pushed=None)
    row = session.get(SavedFilter, int(editing)) if editing.isdigit() else None
    taken = {saved.row.slot for saved in every if row is None or saved.row.id != row.id}
    free = [index for index in range(len(SWATCHES)) if index not in taken]
    return {
        "saved_buttons": buttons,
        "bullets": bullets,
        "color_blind": saved_filters.color_blind(session),
        "swatches": SWATCHES,
        "save_form": {
            "action": f"/settings/filters/{row.id}" if row is not None else "/settings/filters",
            "editing": row,
            "label": row.label if row is not None else "",
            "slot": row.slot if row is not None else (free[0] if free else None),
            "free_slots": free,
            "full": row is None and len(every) >= saved_filters.MAX_FILTERS,
            "max": saved_filters.MAX_FILTERS,
            "params": filter_params(plain),
        },
    }


@page("/export.csv")
def export_csv(request: Request) -> Response:
    """Every posting the current view and filters show, as CSV (no paging)."""
    state = _state(request)
    query = parse_query(request.query_params)
    now = state.clock()
    with state.sessions() as session:
        ids = dashboard.matching_ids(session, query, now=now, hidden=_hidden(request, session))
        rows = dashboard.load_postings(session, ids)
        zone = state.current().zone
        text = postings_csv(rows, zone=zone, redact=state.templates.env.finalize)
    name = f"jobpulse-{query.status.lower()}-{now.astimezone(zone):%Y-%m-%d}.csv"
    return Response(
        text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


MAX_BULK = 5000
# Applied is your record of applications: never emptied in bulk.
BULK_VIEWS = ("New", "Saved")


def _bulk_ids(value: str) -> list[int]:
    try:
        ids = [int(part) for part in value.split(",") if part.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids must be whole numbers") from None
    if not ids or len(ids) > MAX_BULK:
        raise HTTPException(status_code=400, detail=f"between 1 and {MAX_BULK} postings at once")
    return ids


@actions.post("/postings/bulk-status", response_class=HTMLResponse)
def bulk_status(
    request: Request,
    status: Annotated[str, Form()],
    filters: Annotated[str, Form()] = "",
    as_of: Annotated[str, Form()] = "",
    ids: Annotated[str, Form()] = "",
    return_to: Annotated[str, Form()] = "",
) -> Response:
    """Dismiss everything the filters showed when the page was rendered (``as_of``), or, with
    ``ids``, put those postings back (the undo)."""
    if status not in STATUSES:
        raise HTTPException(status_code=400, detail=f"unknown status {status!r}")
    state = _state(request)
    now = state.clock()
    with session_scope(state.sessions) as session:
        if ids:
            moved = repo.set_statuses(session, _bulk_ids(ids), status, now=now)
            if not _htmx(request):
                return RedirectResponse(safe_return_to(return_to), status_code=303)
            return HTMLResponse("", headers={"HX-Refresh": "true"})
        if status != "Dismissed":
            raise HTTPException(status_code=400, detail="only Dismiss works on a whole list")
        try:
            shown_at = datetime.fromisoformat(as_of)
        except ValueError:
            raise HTTPException(status_code=400, detail="as_of must be a time") from None
        if shown_at.tzinfo is None:
            raise HTTPException(status_code=400, detail="as_of needs a timezone")
        query = parse_query(dict(parse_qsl(urlsplit(filters).query)))
        if query.status not in BULK_VIEWS:
            raise HTTPException(
                status_code=400, detail=f"{query.status} can't be dismissed in bulk"
            )
        found = dashboard.matching_ids(
            session, query, now=now, hidden=_hidden(request, session), as_of=shown_at
        )[:MAX_BULK]
        moved = repo.set_statuses(session, found, status, now=now)
        if not _htmx(request):
            return RedirectResponse(safe_return_to(return_to), status_code=303)
        return render(
            request,
            "_bulk.html",
            {
                "moved": moved,
                "ids": ",".join(map(str, found)),
                "previous": query.status,
                "status": status,
                "return_to": safe_return_to(return_to),
                "counts": _counts(request, session),
                "oob_counts": True,
            },
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
        previous = repo.set_status(session, posting_id, status, now=state.clock())
        if previous is None:
            raise HTTPException(status_code=404, detail="no such posting")
        if not _htmx(request):
            return RedirectResponse(safe_return_to(return_to), status_code=303)
        row = session.get(PostingRow, posting_id)
        counts = _counts(request, session)
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
    snapshot = state.current()
    with state.sessions() as session:
        targets = dashboard.known_targets(
            session, blocklist=_blocklist(request, session), feeds=snapshot.feeds
        )
        return render(
            request,
            "health.html",
            {
                "view": "health",
                "targets": dashboard.health_overview(session, now=state.clock(), targets=targets),
                "incidents": dashboard.open_incidents(session),
                "failures_needed": snapshot.config.health.consecutive_failures,
                "quiet_hours": f"{snapshot.config.health.zero_hours:g}",
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
    blocklist = _blocklist(request, session)
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
                "config_blocked": _state(request).current().config.blocklist.companies,
                "dashboard_blocked": repo.blocked_companies(session, kind="company"),
                "marked_agencies": repo.blocked_companies(session, kind="agency"),
                "hide_agencies": _state(request).current().config.hide_agencies,
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
    profile_blocks = state.current().blocklist
    with state.sessions() as session:
        known = (ref.ats, ref.token.lower()) in repo.known_companies(session)
        blocklist = _blocklist(request, session)
    if known:
        return Detection(url, f"{board} is already on your watchlist.", "info", ref)
    if profile_blocks.blocks_board(ref.ats, ref.token):
        return Detection(url, f"{board} is on your blocklist (blocklist.boards on Settings).")
    validation = await state.detector.validate(ref)
    if not validation.ok:
        return Detection(url, f"{board} couldn't be validated: {validation.error}", ref=ref)
    name = validation.name or default_board_name(ref)
    if profile_blocks.blocks_company(name):
        return Detection(
            url, f"{name} is on your blocklist (blocklist.companies on Settings).", ref=ref
        )
    if blocklist.blocks_company(name):
        return Detection(
            url, f"{name} is blocked (Blocked companies, below; unblock it there).", ref=ref
        )
    return Detection(
        url,
        ref=validation.ref,
        validation=validation,
        name=name,
        tone="success",
        warning=_size_warning(state.current().config, ref, validation.job_count),
    )


def _size_warning(config: InfraConfig, ref: AtsRef, job_count: int) -> str | None:
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
    profile_blocks = state.current().blocklist
    if profile_blocks.blocks_board(ats, token) or profile_blocks.blocks_company(name):
        raise HTTPException(
            status_code=400, detail=f"{ats}/{token} ({name}) is on your blocklist (Settings)"
        )
    source_url = url.strip() if url.strip().startswith(("https://", "http://")) else None
    with session_scope(state.sessions) as session:
        if _blocklist(request, session).blocks_company(name):
            raise HTTPException(
                status_code=400, detail=f"{name} is blocked; unblock it on the Watchlist page"
            )
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


@actions.post("/companies/block", response_class=HTMLResponse)
def block_company(
    request: Request,
    company: Annotated[str, Form()] = "",
    posting_id: Annotated[str, Form()] = "",
    return_to: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "company",
) -> Response:
    """Block a company from its card: hidden from the dashboard (Applied aside) at once, and
    from the worker's next cycle never pushed, polled or discovered. ``kind=agency`` (the
    card's dialog asks) marks it a staffing agency instead: hidden the same way, but only
    while hide_agencies is on."""
    state = _state(request)
    name = " ".join(company.split())
    if not normalize_company(name):
        raise HTTPException(status_code=400, detail="the posting has no company to block")
    agency = kind == "agency"
    hides = not agency or state.current().config.hide_agencies
    with session_scope(state.sessions) as session:
        repo.block_company(session, name, now=state.clock(), kind="agency" if agency else "company")
        hidden = dashboard.company_postings(session, name) if hides else 0
        if not _htmx(request):
            return RedirectResponse(safe_return_to(return_to), status_code=303)
        card_id = posting_id if posting_id.isdigit() else ""
        response = render(
            request,
            "_blocked.html",
            {
                "company": name,
                "hidden_count": hidden,
                "posting_id": card_id,
                "counts": _counts(request, session),
                "oob_counts": True,
                "agency": agency,
                "hides": hides,
            },
        )
    if hides:  # static/app.js takes the company's other cards off the page
        event = {"company": company_key(name), "keep": f"posting-{card_id}"}
        response.headers["HX-Trigger"] = json.dumps({"jobpulse:blocked": event})
    return response


@actions.post("/companies/unblock", response_class=HTMLResponse)
def unblock_company(
    request: Request,
    company: Annotated[str, Form()] = "",
    return_to: Annotated[str, Form()] = "",
) -> Response:
    with session_scope(_state(request).sessions) as session:
        repo.unblock_company(session, company)
    if not _htmx(request):
        return RedirectResponse(safe_return_to(return_to, "/watchlist"), status_code=303)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@actions.post("/watchlist/{company_id}/active", response_class=HTMLResponse)
def set_active(request: Request, company_id: int, active: Annotated[str, Form()] = "") -> Response:
    with session_scope(_state(request).sessions) as session:
        _editable_company(session, company_id)
        repo.update_company(session, company_id, active=active.strip() in ("1", "true", "on"))
        return _company_response(request, session, company_id)


# --- settings ------------------------------------------------------------------------------

SETTINGS_TABS: tuple[tuple[str, str], ...] = (
    ("notifications", "Notifications"),
    ("search", "Job search"),
    ("filters", "Saved filters"),
    ("sources", "Sources & schedule"),
    ("security", "Security"),
    ("advanced", "Advanced"),
)
TAB_NAMES = frozenset(name for name, _ in SETTINGS_TABS)
NOTIFY_SECRETS = ("ntfy_topic", "ntfy_token", "discord_webhook_url")
SECRET_TABS = {name: "notifications" for name in NOTIFY_SECRETS} | {"llm_api_key": "advanced"}
SECRET_STATES = {"settings": "set", "unset": "not set"}


def _settings_page(
    request: Request,
    session: Session,
    tab: str,
    *,
    status_code: int = 200,
    extra: dict[str, Any] | None = None,
) -> HTMLResponse:
    """One tab of Settings; every tab's context is cheap, so all of it is built."""
    state = _state(request)
    snapshot = state.current()
    secrets = state.store.resolve(session)
    filters = saved_filters.list_filters(session)

    def secret_rows(names: tuple[str, ...]) -> list[dict[str, str]]:
        return [
            {
                "name": name,
                "label": SECRET_LABELS[name],
                "source": secrets.sources[name],
                "state": SECRET_STATES[secrets.sources[name]],
            }
            for name in names
        ]

    return render(
        request,
        "settings.html",
        {
            "view": "settings",
            "tab": tab,
            "tabs": SETTINGS_TABS,
            "notice": None,
            "notice_tone": "info",
            "errors": [],
            # Notifications
            "notify_secrets": secret_rows(NOTIFY_SECRETS),
            "channels": snapshot.config.notify.channels,
            "color_blind": saved_filters.color_blind(session),
            "swatches": SWATCHES,
            # Job search
            "setup_answers": snapshot.config.answers,
            "setup_onboarded": snapshot.onboarded,
            "resume": resume_db.get(session),
            # Saved filters
            "filters": filters,
            "summaries": {
                saved.row.id: filter_summary(saved.query)
                for saved in filters
                if saved.query is not None
            },
            "edit_links": {
                saved.row.id: link(replace(saved.query, status="New", sort="score", page=1))
                + f"&editing={saved.row.id}"
                for saved in filters
                if saved.query is not None
            },
            "per_cycle": snapshot.config.notify.saved_filters.max_per_cycle,
            # Sources & schedule
            "sources_form": snapshot.config.sources.model_dump(),
            # Security
            "password_set": password_hash(session) is not None,
            "min_password": MIN_PASSWORD_CHARS,
            # Advanced
            "advanced_secrets": secret_rows(("llm_api_key",)),
            "profile_text": profile_yaml(split(snapshot.config)[1]),
            "profile_revision": snapshot.revision,
            **(extra or {}),
        },
        session=session,
        status_code=status_code,
    )


def _back_to_settings(request: Request, tab: str) -> Response:
    target = f"/settings/{tab}"
    if _htmx(request):
        return Response(headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


def _settings_error(
    request: Request, session: Session, tab: str, status_code: int, **extra: Any
) -> HTMLResponse:
    return _settings_page(request, session, tab, status_code=status_code, extra=extra)


@page("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    with _state(request).sessions() as session:
        return _settings_page(request, session, SETTINGS_TABS[0][0])


@page("/settings/{tab}", response_class=HTMLResponse)
def settings_tab(request: Request, tab: str) -> HTMLResponse:
    if tab not in TAB_NAMES:
        raise HTTPException(status_code=404, detail="no such settings page")
    with _state(request).sessions() as session:
        return _settings_page(request, session, tab)


def _save_profile_part(
    request: Request, session: Session, revision: str, update: dict[str, Any]
) -> None:
    """Store the profile with ``update`` applied; ``StaleProfile`` when it moved on since
    the page was loaded. A profile not onboarded yet stays a draft."""
    state = _state(request)
    stored = profile_db.get(session)
    profile = (stored.profile or split(state.current().config)[1]).model_copy(update=update)
    profile_db.save(
        session,
        profile,
        now=state.clock(),
        expected_revision=int(revision) if revision.isdigit() else -1,
        onboarded=stored.onboarded,
    )


# --- settings: notifications ----------------------------------------------------------------


@actions.post("/settings/secrets/{name}", response_class=HTMLResponse)
def save_secret(request: Request, name: str, value: Annotated[str, Form()] = "") -> Response:
    state = _state(request)
    tab = SECRET_TABS.get(name, "notifications")
    with session_scope(state.sessions) as session:
        try:
            state.store.save(session, name, value, now=state.clock())
        except SecretError as exc:
            session.rollback()
            return _settings_error(request, session, tab, 400, notice=str(exc), notice_tone="error")
    return _back_to_settings(request, tab)


@actions.post("/settings/secrets/{name}/clear")
def clear_secret(request: Request, name: str) -> Response:
    state = _state(request)
    with session_scope(state.sessions) as session:
        try:
            state.store.clear(session, name)
        except SecretError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
    return _back_to_settings(request, SECRET_TABS.get(name, "notifications"))


@actions.post("/settings/channels", response_class=HTMLResponse)
async def save_channels(request: Request) -> Response:
    """Where pushes go: ntfy, Discord, both or neither."""
    form = await request.form()
    channels = [name for name in ("ntfy", "discord") if form.get(name) == "1"]
    revision = str(form.get("revision", ""))
    state = _state(request)
    notify = state.current().config.notify.model_copy(update={"channels": channels})
    with session_scope(state.sessions) as session:
        try:
            _save_profile_part(request, session, revision, {"notify": notify})
        except StaleProfile as exc:
            session.rollback()
            return _settings_error(
                request, session, "notifications", 409, notice=f"{exc}; try again.",
                notice_tone="error",
            )  # fmt: skip
    return _back_to_settings(request, "notifications")


@actions.post("/settings/notify/test", response_class=HTMLResponse)
async def test_push(request: Request) -> HTMLResponse:
    """One test push through every channel that's set up, with today's secrets."""
    state = _state(request)
    status_code = 200
    try:
        names = await send_test(LiveNotifier(state.http, state.sessions, state.store))
    except NotifierError as exc:
        notice, tone, status_code = f"The test push failed: {exc}", "error", 502
    else:
        notice = f"Test push sent via {', '.join(names)}." if names else NO_CHANNEL
        tone = "success" if names else "error"
    with state.sessions() as session:
        return _settings_error(
            request, session, "notifications", status_code, notice=notice, notice_tone=tone
        )


@actions.post("/settings/color-blind")
def set_color_blind(request: Request, color_blind: Annotated[str, Form()] = "") -> Response:
    state = _state(request)
    with session_scope(state.sessions) as session:
        saved_filters.set_color_blind(
            session, color_blind.strip() in ("1", "true", "on"), now=state.clock()
        )
    return _back_to_settings(request, "notifications")


# --- settings: job search (resume) ----------------------------------------------------------


@actions.post("/settings/resume", response_class=HTMLResponse)
async def upload_resume(request: Request, resume: Annotated[UploadFile, File()]) -> Response:
    """Store an uploaded resume, as `jobpulse resume set` does: the worker embeds it and
    re-scores open matches on its next cycle."""
    state = _state(request)
    filename = Path(resume.filename or "").name
    data = await resume.read(MAX_RESUME_BYTES + 1)
    try:
        read = await asyncio.to_thread(read_resume_bytes, filename, data)
        lines = resume_lines(read.text)
        if not lines:
            raise ResumeFileError(f"no content lines found in {filename}: use bullets or sentences")
    except ResumeFileError as exc:
        with state.sessions() as session:
            return _settings_error(
                request, session, "search", 400, notice=str(exc), notice_tone="error"
            )
    with session_scope(state.sessions) as session:
        resume_db.put(
            session,
            filename=filename,
            media_type=read.media_type,
            text=read.text,
            lines=lines,
            now=state.clock(),
        )
    return _back_to_settings(request, "search")


@actions.post("/settings/resume/clear")
def clear_resume(request: Request) -> Response:
    """Delete the resume; open matches go back to the stack keywords at once."""
    state = _state(request)
    config = state.current().config
    with session_scope(state.sessions) as session:
        if resume_db.clear(session):
            restore_stack(session, rules=filter_rules(config), weights=scoring_weights(config))
    return _back_to_settings(request, "search")


# --- settings: saved filters (made and edited from the dashboard's filter bar) --------------


async def _filter_form(request: Request) -> tuple[str, int, dict[str, str], PostingQuery]:
    """Label, color slot (-1 when unreadable) and filters from the dashboard's save form."""
    form = await request.form()
    values = {key: value for key, value in form.items() if isinstance(value, str)}
    query = parse_query({key: value for key, value in values.items() if key != "view"})
    slot_text = values.get("slot", "").strip()
    slot = int(slot_text) if slot_text.isdigit() else -1
    return values.get("label", ""), slot, filter_params(query), query


def _saved_response(request: Request, query: PostingQuery) -> Response:
    """Back to the dashboard, showing what the saved filter keeps."""
    target = link(replace(query, status="New", sort="score", page=1))
    if _htmx(request):
        return Response(headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


@actions.post("/settings/filters")
async def create_saved_filter(request: Request) -> Response:
    label, slot, params, query = await _filter_form(request)
    state = _state(request)
    with session_scope(state.sessions) as session:
        try:
            saved_filters.create(session, label=label, slot=slot, params=params, now=state.clock())
        except SavedFilterError as exc:
            session.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from None
    return _saved_response(request, query)


@actions.post("/settings/filters/{filter_id}")
async def update_saved_filter(request: Request, filter_id: int) -> Response:
    label, slot, params, query = await _filter_form(request)
    state = _state(request)
    with session_scope(state.sessions) as session:
        if session.get(SavedFilter, filter_id) is None:
            raise HTTPException(status_code=404, detail="no such saved filter")
        try:
            saved_filters.update(
                session, filter_id, label=label, slot=slot, params=params, now=state.clock()
            )
        except SavedFilterError as exc:
            session.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from None
    return _saved_response(request, query)


@actions.post("/settings/filters/{filter_id}/active")
def switch_saved_filter(
    request: Request, filter_id: int, active: Annotated[str, Form()] = ""
) -> Response:
    state = _state(request)
    with session_scope(state.sessions) as session:
        try:
            saved_filters.set_active(
                session, filter_id, active.strip() in ("1", "true", "on"), now=state.clock()
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="no such saved filter") from None
    return _back_to_settings(request, "filters")


@actions.post("/settings/filters/{filter_id}/delete")
def delete_saved_filter(request: Request, filter_id: int) -> Response:
    with session_scope(_state(request).sessions) as session:
        try:
            saved_filters.delete(session, filter_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="no such saved filter") from None
    return _back_to_settings(request, "filters")


@actions.post("/saved-filters/{filter_id}/viewed")
def saved_filter_viewed(request: Request, filter_id: int) -> Response:
    """Sent by static/app.js when a saved filter's button is clicked: its badge starts over."""
    state = _state(request)
    with session_scope(state.sessions) as session:
        try:
            saved_filters.mark_viewed(session, filter_id, now=state.clock())
        except LookupError:
            raise HTTPException(status_code=404, detail="no such saved filter") from None
    return Response(status_code=204)


# --- settings: sources & schedule -----------------------------------------------------------

SOURCE_SWITCHES = (
    "linkedin_job_pages",
    "simplify_feed",
    "discovery",
    "seed_updates",
    "health_alerts",
    "resume_matching",
)


@actions.post("/settings/sources", response_class=HTMLResponse)
async def save_sources(request: Request) -> Response:
    """Settings → Sources & schedule. The worker rebuilds its schedule within a minute."""
    form = await request.form()
    values: dict[str, Any] = {
        name: value
        for name, value in form.items()
        if isinstance(value, str) and name in SourceSettings.model_fields
    }
    for name in SOURCE_SWITCHES:
        values[name] = form.get(name) == "1"
    state = _state(request)
    with session_scope(state.sessions) as session:
        try:
            sources = SourceSettings.model_validate(values)
            _save_profile_part(
                request, session, str(form.get("revision", "")), {"sources": sources}
            )
        except ValidationError as exc:
            session.rollback()
            return _settings_error(
                request, session, "sources", 400, errors=describe_errors(exc),
                sources_form={**state.current().config.sources.model_dump(), **values},
            )  # fmt: skip
        except StaleProfile as exc:
            session.rollback()
            return _settings_error(
                request, session, "sources", 409, notice=f"{exc}; try again.",
                notice_tone="error",
            )  # fmt: skip
    return _back_to_settings(request, "sources")


# --- settings: security ---------------------------------------------------------------------


@actions.post("/settings/password", response_class=HTMLResponse)
def save_password(
    request: Request,
    password: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> Response:
    """Turn on (or change) the dashboard password. The browser asks for it from the next
    page on; any user name works."""
    state = _state(request)
    with session_scope(state.sessions) as session:
        try:
            if password != confirm:
                raise SecretError("the two passwords don't match")
            set_password(session, password, now=state.clock())
        except SecretError as exc:
            session.rollback()
            return _settings_error(
                request, session, "security", 400, notice=str(exc), notice_tone="error"
            )
    return _back_to_settings(request, "security")


@actions.post("/settings/password/clear")
def remove_password(request: Request) -> Response:
    with session_scope(_state(request).sessions) as session:
        clear_password(session)
    return _back_to_settings(request, "security")


# --- settings: advanced (the profile as YAML) -----------------------------------------------


@actions.post("/settings/profile", response_class=HTMLResponse)
def save_profile(
    request: Request,
    profile: Annotated[str, Form()] = "",
    revision: Annotated[str, Form()] = "",
) -> Response:
    """The Advanced editor: the whole profile as YAML. Errors are listed by path; a profile
    that changed since the page was loaded answers 409, keeping the text you sent."""
    state = _state(request)
    with session_scope(state.sessions) as session:
        try:
            parsed = note_custom(parse_profile_yaml(profile))
            profile_db.save(
                session,
                parsed,
                now=state.clock(),
                expected_revision=int(revision) if revision.isdigit() else -1,
            )
        except ProfileError as exc:
            session.rollback()
            return _settings_error(
                request, session, "advanced", 400, profile_text=profile, errors=exc.errors
            )
        except StaleProfile as exc:
            session.rollback()
            problem = FieldError("", f"{exc}. Copy your changes, reload, and make them again.")
            return _settings_error(
                request, session, "advanced", 409, profile_text=profile, errors=[problem]
            )
    return _back_to_settings(request, "advanced")
