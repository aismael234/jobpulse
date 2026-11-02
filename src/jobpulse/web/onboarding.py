"""The setup wizard (M8): resume → roles and skills → level → locations → goals →
notifications → review, as plain forms with 303 redirects (HTMX only enhances). Each step
saves the answers in the profile and rebuilds the sections they drive
(``onboarding.apply_answers``); finishing marks the profile onboarded and, the first time,
adds the board seed in the background.

Until onboarding is done, every page but the wizard, Settings, static files and /healthz
redirects here (``OnboardingRedirect``). An onboarded profile with answers can come back to
change them from Settings; an imported one has none and never sees the wizard.
"""

import asyncio
import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from jobpulse.config import Profile, ProfileError, SetupAnswers, describe_errors
from jobpulse.core.countries import country_name, subdivisions
from jobpulse.core.places import metro_options, place_label
from jobpulse.core.profile_builder import searches, workday_keywords
from jobpulse.core.resume_facts import read_resume
from jobpulse.core.roles import LEVELS, LEVELS_BY_KEY, ROLE_FAMILIES
from jobpulse.core.similarity import resume_lines
from jobpulse.core.skills import SKILLS
from jobpulse.credentials import SecretError
from jobpulse.data import agency_names
from jobpulse.db import profile as profile_db
from jobpulse.db import resume as resume_db
from jobpulse.db.profile import StoredProfile
from jobpulse.db.session import session_scope
from jobpulse.notify.base import NotifierError
from jobpulse.notify.live import NO_CHANNEL, LiveNotifier, send_test
from jobpulse.onboarding import apply_answers, apply_seed, generate_topic, prefill
from jobpulse.resume_files import MAX_BYTES as MAX_RESUME_BYTES
from jobpulse.resume_files import ResumeFileError, read_resume_bytes
from jobpulse.web.routes import actions, page, render

log = logging.getLogger(__name__)

STEPS: tuple[tuple[str, str], ...] = (
    ("resume", "Resume"),
    ("roles", "Roles and skills"),
    ("level", "Level"),
    ("locations", "Locations"),
    ("goals", "Pay and companies"),
    ("notifications", "Notifications"),
    ("review", "Review"),
)
STEP_NAMES = tuple(name for name, _ in STEPS)
EXEMPT_PREFIXES = ("/setup", "/settings", "/static/")
EXEMPT_PATHS = frozenset({"/healthz", "/favicon.ico"})
MAX_COMPANIES = 50
MAX_COMPANY_CHARS = 200


class OnboardingRedirect:
    """Sends every page to the wizard until a profile is onboarded; HTMX requests get
    ``HX-Redirect``. The wizard, Settings, static files and /healthz pass."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or path in EXEMPT_PATHS
            or path.startswith(EXEMPT_PREFIXES)
            or await _onboarded(scope)
        ):
            await self.app(scope, receive, send)
            return
        if Headers(scope=scope).get("hx-request") == "true":
            response: Response = Response(status_code=200, headers={"HX-Redirect": "/setup"})
        else:
            response = RedirectResponse("/setup", status_code=303)
        await response(scope, receive, send)


async def _onboarded(scope: Scope) -> bool:
    state = scope["app"].state.web
    snapshot = await asyncio.to_thread(state.current)
    return bool(snapshot.onboarded)


# --- helpers --------------------------------------------------------------------------------


def _state(request: Request) -> Any:
    return request.app.state.web


def _stored(session: Session) -> StoredProfile:
    return profile_db.get(session)


def _answers(stored: StoredProfile) -> SetupAnswers:
    if stored.profile is not None and stored.profile.answers is not None:
        return stored.profile.answers
    return SetupAnswers()


def _editable(stored: StoredProfile) -> None:
    """An onboarded profile without answers (imported, or hand-written) is all custom."""
    if stored.onboarded and (stored.profile is None or stored.profile.answers is None):
        raise HTTPException(
            status_code=409,
            detail="your profile was imported or written by hand, so the setup can't change "
            "it; edit it on Settings (Advanced)",
        )


def _save(request: Request, session: Session, answers: SetupAnswers) -> None:
    """Store the answers and the sections they build. Raises ``ProfileError``."""
    stored = _stored(session)
    profile = apply_answers(stored.profile or Profile(), answers)
    profile_db.save(session, profile, now=_state(request).clock(), onboarded=stored.onboarded)


def _next(step: str) -> str:
    index = STEP_NAMES.index(step)
    return f"/setup/{STEP_NAMES[min(index + 1, len(STEP_NAMES) - 1)]}"


def _lines(value: str, *, field: str) -> list[str]:
    names = [" ".join(line.split()) for line in value.splitlines()]
    names = [name for name in names if name]
    if len(names) > MAX_COMPANIES or any(len(name) > MAX_COMPANY_CHARS for name in names):
        raise ValueError(f"{field}: at most {MAX_COMPANIES} names, one per line")
    return list(dict.fromkeys(names))


def _step_page(
    request: Request,
    session: Session,
    step: str,
    *,
    answers: SetupAnswers | None = None,
    error: str | None = None,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    stored = _stored(session)
    answers = answers if answers is not None else _answers(stored)
    context: dict[str, Any] = {
        "step": step,
        "steps": STEPS,
        "answers": answers,
        "editing": stored.onboarded,
        "error": error,
        "resume": resume_db.get(session),
        **_choices(step, answers),
        **extra,
    }
    return render(request, f"setup/{step}.html", context, status_code=status_code)


def _choices(step: str, answers: SetupAnswers) -> dict[str, Any]:
    if step == "roles":
        groups: dict[str, list[Any]] = {}
        for skill in SKILLS:
            groups.setdefault(skill.group, []).append(skill)
        return {"families": ROLE_FAMILIES, "skill_groups": list(groups.items())}
    if step == "level":
        return {"levels": LEVELS}
    if step == "locations":
        return {
            "metros": metro_options(),
            "states": subdivisions("US"),
            "country_names": [country_name(code) for code in answers.countries],
        }
    if step == "goals":
        return {"agencies": agency_names()}
    if step == "review":
        core = answers.to_core()
        level = LEVELS_BY_KEY[answers.level]
        return {
            "family_labels": [f.label for f in ROLE_FAMILIES if f.key in answers.roles],
            "level_label": level.label,
            "max_years": answers.max_years if answers.max_years is not None else level.max_years,
            "place_labels": [place_label(key) for key in answers.places],
            "country_names": [country_name(code) for code in answers.countries],
            "planned_searches": searches(core),
            "keywords": workday_keywords(core),
            "skill_labels": {skill.key: skill.label for skill in SKILLS},
        }
    return {}


async def _form(request: Request) -> FormData:
    return await request.form()


def _invalid(exc: ValidationError | ProfileError | ValueError) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{error.field}: {error.message}" if error.field else error.message
            for error in describe_errors(exc)
        )
    return str(exc)


# --- pages ----------------------------------------------------------------------------------


@page("/setup", response_class=HTMLResponse)
def setup_start(request: Request) -> Response:
    with _state(request).sessions() as session:
        stored = _stored(session)
    _editable(stored)
    first = "roles" if stored.onboarded else "resume"
    return RedirectResponse(f"/setup/{first}", status_code=303)


@page("/setup/{step}", response_class=HTMLResponse)
def setup_step(request: Request, step: str) -> Response:
    if step not in STEP_NAMES:
        raise HTTPException(status_code=404, detail="no such setup step")
    state = _state(request)
    with state.sessions() as session:
        stored = _stored(session)
        _editable(stored)
        extra: dict[str, Any] = {}
        if step == "notifications":
            extra = _notification_context(state, session)
        return _step_page(request, session, step, **extra)


def _notification_context(state: Any, session: Session) -> dict[str, Any]:
    """Whether each secret is set, and for ntfy without one a fresh topic to subscribe to.
    The topic shows here once, before it's saved; from then on it's masked like any
    secret."""
    secrets = state.store.resolve(session)
    snapshot = state.current()
    topic_set = bool(secrets.get("ntfy_topic"))
    return {
        "topic_source": secrets.sources["ntfy_topic"],
        "webhook_source": secrets.sources["discord_webhook_url"],
        "new_topic": None if topic_set else generate_topic(),
        "ntfy_base": snapshot.config.notify.ntfy.base_url,
        "contact_email": snapshot.config.contact_email or "",
        "notice": None,
        "notice_tone": "info",
    }


# --- actions --------------------------------------------------------------------------------


@actions.post("/setup/resume", response_class=HTMLResponse)
async def setup_resume(
    request: Request, resume: Annotated[UploadFile | None, File()] = None
) -> Response:
    """Store the resume, as Settings does, and prefill the answers it suggests. Without a
    file, go on."""
    state = _state(request)
    with state.sessions() as session:
        _editable(_stored(session))
    if resume is None or not resume.filename:
        return RedirectResponse("/setup/roles", status_code=303)
    filename = Path(resume.filename).name
    data = await resume.read(MAX_RESUME_BYTES + 1)
    try:
        read = await asyncio.to_thread(read_resume_bytes, filename, data)
        lines = resume_lines(read.text)
        if not lines:
            raise ResumeFileError(f"no content lines found in {filename}: use bullets or sentences")
    except ResumeFileError as exc:
        with state.sessions() as session:
            return _step_page(request, session, "resume", error=str(exc), status_code=400)
    now = state.clock()
    facts = read_resume(read.text, today=now.date())
    with session_scope(state.sessions) as session:
        resume_db.put(
            session,
            filename=filename,
            media_type=read.media_type,
            text=read.text,
            lines=lines,
            now=now,
        )
        _save(request, session, prefill(_answers(_stored(session)), facts))
    return RedirectResponse("/setup/roles", status_code=303)


def _parse(step: str, form: FormData, answers: SetupAnswers) -> dict[str, Any]:
    """The step's fields from the form, as an update to the answers."""

    def values(name: str) -> list[str]:
        return [value for value in form.getlist(name) if isinstance(value, str)]

    def text(name: str) -> str:
        value = form.get(name)
        return value.strip() if isinstance(value, str) else ""

    if step == "roles":
        roles = values("roles")
        if not roles:
            raise ValueError("pick at least one kind of role")
        skills = {
            skill.key: choice
            for skill in SKILLS
            if (choice := text(f"skill-{skill.key}")) in ("main", "also")
        }
        return {"roles": roles, "skills": skills}
    if step == "level":
        years = text("max_years")
        if years and not years.isdigit():
            raise ValueError("max_years: a whole number of years, or blank")
        return {"level": text("level") or "mid", "max_years": int(years) if years else None}
    if step == "locations":
        countries = [part.strip() for part in text("countries").split(",") if part.strip()]
        return {
            "countries": countries,
            "places": values("places"),
            "remote_ok": text("remote_ok") == "1",
            "_timezone": text("timezone") or None,
        }
    if step == "goals":
        pay = text("min_pay").replace(",", "").removesuffix("k").removesuffix("K")
        if pay and not pay.isdigit():
            raise ValueError("min_pay: thousands of dollars a year, such as 120, or blank")
        return {
            "min_pay": int(pay) if pay else None,
            "dream": _lines(text("dream"), field="dream"),
            "avoid": _lines(text("avoid"), field="avoid"),
            "hide_agencies": text("hide_agencies") == "1",
            "job_boards": text("job_boards") == "1",
        }
    raise HTTPException(status_code=404, detail="no such setup step")


@actions.post("/setup/notifications", response_class=HTMLResponse)
async def setup_notifications(request: Request) -> Response:
    """The channel, its secret (a generated ntfy topic, or a Discord webhook) and the contact
    email; ``send=test`` sends a test push and comes back here."""
    state = _state(request)
    form = await _form(request)

    def text(name: str) -> str:
        value = form.get(name)
        return value.strip() if isinstance(value, str) else ""

    channel = text("channel") or "ntfy"
    with session_scope(state.sessions) as session:
        stored = _stored(session)
        _editable(stored)
        answers = _answers(stored)
        error: str | None = None
        try:
            answers = SetupAnswers.model_validate(
                {**answers.model_dump(mode="json"), "channel": channel}
            )
            topic = text("topic")
            if channel == "ntfy" and topic and not state.store.resolve(session).get("ntfy_topic"):
                state.store.save(session, "ntfy_topic", topic, now=state.clock())
            if channel == "discord" and (webhook := text("webhook")):
                state.store.save(session, "discord_webhook_url", webhook, now=state.clock())
            _save(request, session, answers)
            _contact(request, session, text("contact_email"))
        except (ValidationError, ProfileError, SecretError, ValueError) as exc:
            error = _invalid(exc)
        if error is not None:
            session.rollback()
            return _step_page(
                request,
                session,
                "notifications",
                answers=answers,
                error=error,
                status_code=400,
                **_notification_context(state, session),
            )
    if text("send") != "test":
        return RedirectResponse(_next("notifications"), status_code=303)
    try:
        names = await send_test(LiveNotifier(state.http, state.sessions, state.store))
    except NotifierError as exc:
        notice, tone = f"The test push failed: {exc}", "error"
    else:
        notice = f"Test push sent via {', '.join(names)}." if names else NO_CHANNEL
        tone = "success" if names else "error"
    with state.sessions() as session:
        context = {**_notification_context(state, session), "notice": notice, "notice_tone": tone}
        return _step_page(request, session, "notifications", **context)


def _contact(request: Request, session: Session, email: str) -> None:
    """The contact email goes straight into the profile (it isn't an answer)."""
    stored = _stored(session)
    if stored.profile is None or (email or None) == stored.profile.contact_email:
        return
    data = stored.profile.model_dump(mode="json")
    data["contact_email"] = email or None
    profile = Profile.model_validate(data)
    profile_db.save(session, profile, now=_state(request).clock(), onboarded=stored.onboarded)


@actions.post("/setup/review", response_class=HTMLResponse)
async def setup_finish(request: Request) -> Response:
    """Mark the profile onboarded. The first time, the packaged seed's boards are added in
    the background (each Workday board searched with keywords from your roles); their first
    poll is a silent baseline."""
    state = _state(request)
    with session_scope(state.sessions) as session:
        stored = _stored(session)
        _editable(stored)
        answers = _answers(stored)
        if not answers.roles:
            return RedirectResponse("/setup/roles", status_code=303)
        profile = apply_answers(stored.profile or Profile(), answers)
        profile_db.save(session, profile, now=state.clock(), onboarded=True)
    first_time = not stored.onboarded
    if first_time and state.detector is not None:
        config = state.current().config
        state.spawn(_seed(state, config))
    return RedirectResponse("/" if first_time else "/settings#setup", status_code=303)


async def _seed(state: Any, config: Any) -> None:
    try:
        await apply_seed(state.sessions, config, state.detector, now=state.clock())
    except Exception:
        log.exception("seed: applying the packaged seed after onboarding failed")


@actions.post("/setup/{step}", response_class=HTMLResponse)
async def setup_save(request: Request, step: str) -> Response:
    state = _state(request)
    form = await _form(request)
    with session_scope(state.sessions) as session:
        stored = _stored(session)
        _editable(stored)
        answers = _answers(stored)
        try:
            update = _parse(step, form, answers)
            timezone = update.pop("_timezone", None)
            answers = SetupAnswers.model_validate({**answers.model_dump(mode="json"), **update})
            _save(request, session, answers)
            if timezone is not None:
                _timezone(request, session, timezone)
        except (ValidationError, ProfileError, ValueError) as exc:
            session.rollback()
            return _step_page(request, session, step, error=_invalid(exc), status_code=400)
    return RedirectResponse(_next(step), status_code=303)


def _timezone(request: Request, session: Session, timezone: str) -> None:
    stored = _stored(session)
    if stored.profile is None or stored.profile.timezone == timezone:
        return
    data = stored.profile.model_dump(mode="json")
    data["timezone"] = timezone
    try:
        profile = Profile.model_validate(data)
    except ValidationError:
        raise ValueError(f"timezone: unknown IANA timezone {timezone!r}") from None
    profile_db.save(session, profile, now=_state(request).clock(), onboarded=stored.onboarded)
