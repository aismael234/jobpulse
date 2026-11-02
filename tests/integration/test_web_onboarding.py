"""M8: the setup wizard. A new install is sent to it; each step saves answers that rebuild
the profile; finishing onboards it and adds the seed once; Settings can come back to change
the answers, and sections edited by hand stay as they are."""

import html
import re
from collections.abc import Callable
from typing import Any

import pytest
import respx
from sqlalchemy import delete

from jobpulse.config import Profile, SetupAnswers, profile_yaml
from jobpulse.db import profile as profile_db
from jobpulse.db import resume as resume_db
from jobpulse.db import secrets as secret_rows
from jobpulse.db.tables import ProfileRow
from jobpulse.onboarding import apply_answers
from tests.factories import NOW
from tests.integration.conftest import Web

RESUME = b"""Jordan Example
Experience
Backend Engineer, Acme
Jan 2022 - Present
- Built Python services with FastAPI and PostgreSQL on AWS.
- Shipped React dashboards.
Education
B.S. Computer Science 2017 - 2021
"""


@pytest.fixture
def fresh(make_web: Callable[..., Web], monkeypatch: pytest.MonkeyPatch) -> Web:
    """A dashboard over a database with no profile yet; the seed is recorded, not applied."""
    web = make_web()
    with web.sessions() as session:
        session.execute(delete(ProfileRow))
        session.commit()
    seeded: list[Any] = []

    async def fake_seed(sessions: Any, config: Any, detector: Any, *, now: Any) -> list[str]:
        seeded.append(config)
        return []

    monkeypatch.setattr("jobpulse.web.onboarding.apply_seed", fake_seed)
    web.seeded = seeded  # type: ignore[attr-defined]
    return web


def stored(web: Web) -> profile_db.StoredProfile:
    with web.sessions() as session:
        return profile_db.get(session)


def post_form(web: Web, path: str, data: dict[str, Any]) -> Any:
    return web.client.post(
        path, data=data, headers={"X-CSRF-Token": web.token}, follow_redirects=False
    )


def page_text(web: Web, path: str) -> str:
    return html.unescape(web.page(path))


# --- the redirect ---------------------------------------------------------------------------


def test_a_new_install_is_sent_to_the_wizard(fresh: Web) -> None:
    for path in ("/", "/watchlist", "/health"):
        response = fresh.client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/setup"
    htmx = fresh.client.get("/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert htmx.headers["HX-Redirect"] == "/setup"
    for path in ("/settings", "/static/app.css", "/healthz", "/setup/resume"):
        assert fresh.client.get(path, follow_redirects=False).status_code == 200, path
    assert fresh.client.get("/setup", follow_redirects=False).headers["location"] == (
        "/setup/resume"
    )


def test_an_onboarded_install_is_never_redirected(web: Web) -> None:
    assert web.client.get("/", follow_redirects=False).status_code == 200


# --- the steps ------------------------------------------------------------------------------


def test_the_whole_wizard(fresh: Web, respx_mock: respx.MockRouter) -> None:
    response = fresh.client.post(
        "/setup/resume",
        files={"resume": ("resume.txt", RESUME, "text/plain")},
        data={"csrf_token": fresh.token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/setup/roles"
    answers = stored(fresh).profile.answers  # type: ignore[union-attr]
    assert answers is not None
    assert "backend" in answers.roles
    assert answers.level == "mid"  # four years
    assert {"python", "fastapi", "postgresql", "aws", "react"} <= set(answers.skills)
    assert not stored(fresh).onboarded
    with fresh.sessions() as session:
        assert resume_db.get(session) is not None

    roles_page = page_text(fresh, "/setup/roles")
    assert re.search(r'value="backend" checked', roles_page)

    form = {"roles": ["backend", "full_stack"], "skill-python": "main"}
    assert post_form(fresh, "/setup/roles", form).headers["location"] == "/setup/level"
    level = post_form(fresh, "/setup/level", {"level": "mid", "max_years": "4"})
    assert level.headers["location"] == "/setup/locations"
    locations = post_form(
        fresh,
        "/setup/locations",
        {
            "countries": "US, Canada",
            "places": ["metro:bay-area", "US-TX"],
            "remote_ok": "1",
            "timezone": "America/Chicago",
        },
    )
    assert locations.headers["location"] == "/setup/goals"
    goals = post_form(
        fresh,
        "/setup/goals",
        {
            "min_pay": "140",
            "dream": "Stripe\nLinear",
            "avoid": "Acme Staffing",
            "hide_agencies": "1",
        },
    )
    assert goals.headers["location"] == "/setup/notifications"

    page = page_text(fresh, "/setup/notifications")
    topic = re.search(r'name="topic" value="([A-Za-z0-9_-]+)"', page)
    assert topic is not None
    ntfy = respx_mock.post(f"https://ntfy.sh/{topic[1]}").respond(200)
    test = post_form(
        fresh,
        "/setup/notifications",
        {"channel": "ntfy", "topic": topic[1], "contact_email": "me@example.com", "send": "test"},
    )
    assert test.status_code == 200
    assert "Test push sent via ntfy." in html.unescape(test.text)
    assert topic[1] not in test.text  # saved: masked from now on
    assert ntfy.call_count == 1
    with fresh.sessions() as session:
        assert secret_rows.get_all(session)["ntfy_topic"] == topic[1]

    review = page_text(fresh, "/setup/review")
    assert "Backend engineer" in review or "backend engineer" in review
    finish = post_form(fresh, "/setup/review", {})
    assert finish.headers["location"] == "/"
    after = stored(fresh)
    assert after.onboarded
    profile = after.profile
    assert profile is not None
    assert profile.timezone == "America/Chicago"
    assert profile.contact_email == "me@example.com"
    assert profile.filters.locations.countries == ["US", "CA"]
    assert profile.filters.locations.places == ["metro:bay-area", "US-TX"]
    assert profile.filters.pay.min_yearly_usd == 140_000
    assert profile.filters.description.max_years_experience == 4
    assert profile.scoring.dream_companies == ["Stripe", "Linear"]
    assert profile.blocklist.companies == ["Acme Staffing"]
    assert profile.notify.channels == ["ntfy"]
    assert [rule.pattern for rule in profile.filters.title.include] == [
        r"\bback[- ]?end\b",
        r"\bfull[- ]?stack\b",
    ]
    assert {search.site for search in profile.searches} == {"linkedin", "indeed"}
    assert len(fresh.seeded) == 1  # type: ignore[attr-defined]
    assert fresh.client.get("/", follow_redirects=False).status_code == 200


def test_a_step_with_an_error_says_so_and_keeps_nothing(fresh: Web) -> None:
    response = post_form(fresh, "/setup/roles", {})
    assert response.status_code == 400
    assert "pick at least one kind of role" in html.unescape(response.text)
    bad = post_form(fresh, "/setup/locations", {"countries": "Atlantis"})
    assert bad.status_code == 400
    assert "Atlantis" in html.unescape(bad.text)
    assert stored(fresh).revision == 0


def test_finishing_without_roles_goes_back_to_roles(fresh: Web) -> None:
    assert post_form(fresh, "/setup/review", {}).headers["location"] == "/setup/roles"
    assert not stored(fresh).onboarded


def test_a_resume_over_2_mb_is_refused(fresh: Web) -> None:
    response = fresh.client.post(
        "/setup/resume",
        files={"resume": ("resume.txt", b"x" * (2 * 1024 * 1024 + 10), "text/plain")},
        data={"csrf_token": fresh.token},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "larger than 2 MB" in html.unescape(response.text)


def test_the_wizard_needs_the_form_token(fresh: Web) -> None:
    response = fresh.client.post("/setup/roles", data={"roles": "backend"})
    assert response.status_code == 403


def test_skipping_the_resume_goes_on(fresh: Web) -> None:
    response = post_form(fresh, "/setup/resume", {})
    assert response.headers["location"] == "/setup/roles"


def test_a_discord_webhook_is_saved_and_masked(fresh: Web, respx_mock: respx.MockRouter) -> None:
    post_form(fresh, "/setup/roles", {"roles": "backend"})
    hook = "https://discord.com/api/webhooks/1/abcdefghijklmnop"
    route = respx_mock.post(hook).respond(204)
    response = post_form(
        fresh, "/setup/notifications", {"channel": "discord", "webhook": hook, "send": "test"}
    )
    assert "Test push sent via discord." in html.unescape(response.text)
    assert hook not in response.text
    assert route.call_count == 1


# --- after onboarding -----------------------------------------------------------------------


def onboarded_with_answers(web: Web, answers: SetupAnswers) -> None:
    with web.sessions() as session:
        profile_db.save(session, apply_answers(Profile(), answers), now=NOW)
        session.commit()


def test_an_imported_profile_never_sees_the_wizard(web: Web) -> None:
    assert web.client.get("/setup", follow_redirects=False).status_code == 409
    assert web.client.get("/setup/roles", follow_redirects=False).status_code == 409
    assert post_form(web, "/setup/roles", {"roles": "backend"}).status_code == 409
    assert "imported or written by hand" in page_text(web, "/settings")


def test_answers_can_change_from_settings_without_a_new_seed(fresh: Web) -> None:
    onboarded_with_answers(fresh, SetupAnswers(roles=["backend"]))
    assert "Change your answers" in page_text(fresh, "/settings")
    assert fresh.client.get("/setup", follow_redirects=False).headers["location"] == (
        "/setup/roles"
    )
    post_form(fresh, "/setup/roles", {"roles": ["frontend"]})
    profile = stored(fresh).profile
    assert profile is not None
    assert [rule.pattern for rule in profile.filters.title.include] == [
        r"\bfront[- ]?end\b|\bweb (developer|engineer)\b|\bui engineer\b"
    ]
    assert post_form(fresh, "/setup/review", {}).headers["location"] == "/settings#setup"
    assert fresh.seeded == []  # type: ignore[attr-defined]


def test_a_section_edited_in_advanced_stays_when_the_answers_change(fresh: Web) -> None:
    onboarded_with_answers(fresh, SetupAnswers(roles=["backend"]))
    profile = stored(fresh).profile
    assert profile is not None
    text = profile_yaml(profile).replace(r"\bback[- ]?end\b", r"\bbackend\b")
    response = fresh.post(
        "/settings/profile", {"profile": text, "revision": str(stored(fresh).revision)}
    )
    assert response.status_code == 200
    edited = stored(fresh).profile
    assert edited is not None
    assert edited.answers is not None
    assert edited.answers.custom == ["filters.title"]
    assert "filters.title" in page_text(fresh, "/settings")

    post_form(fresh, "/setup/roles", {"roles": ["frontend"]})
    after = stored(fresh).profile
    assert after is not None
    assert [rule.pattern for rule in after.filters.title.include] == [r"\bbackend\b"]
    assert after.searches[0].search_term == "frontend engineer"  # not custom: rebuilt
