"""M6 on Settings: secrets (where each comes from; saved ones masked everywhere at once), a
test push, the resume upload, and the profile editor (errors by path, 409 when stale). A
change shows without a restart, and a banner says when the worker can't use the profile."""

import html
import logging
import re
from collections.abc import Callable
from datetime import timedelta
from io import StringIO

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import update

from jobpulse.config import Settings
from jobpulse.credentials import SecretStore
from jobpulse.db import profile as profile_db
from jobpulse.db import resume as resume_db
from jobpulse.db.tables import ProfileRow
from jobpulse.logging_setup import SecretRegistry, configure_logging
from tests.factories import NOW
from tests.integration.conftest import Web, make_config

TOPIC = "jobpulse-Zq3vX9kR2mT7wL4pB8nC1dF6"
OTHER_TOPIC = "jobpulse-Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8"
RESUME = b"""Jane Doe
jane@example.com
Experience
- Built Python services on Postgres for a payments team
- Led a React rewrite of the billing dashboard
"""


def text_of(web: Web, path: str = "/settings") -> str:
    return html.unescape(web.page(path))


def secret_row(page: str, label: str) -> str:
    found = re.search(rf"<li>\s*<span class=\"name\">{label}</span>.*?</li>", page, re.S)
    assert found is not None, label
    return found.group(0)


def upload(web: Web, name: str, data: bytes) -> httpx.Response:
    return web.client.post(  # type: ignore[return-value]
        "/settings/resume",
        files={"resume": (name, data, "application/octet-stream")},
        headers={"X-CSRF-Token": web.token},
        follow_redirects=False,
    )


def revision(web: Web) -> int:
    with web.sessions() as session:
        return profile_db.revision(session)


# --- secrets -------------------------------------------------------------------------------


def test_each_secret_shows_where_it_comes_from(make_web: Callable[..., Web]) -> None:
    store = SecretStore(Settings(_env_file=None, ntfy_topic=SecretStr(TOPIC)))
    web = make_web(store=store)
    page = text_of(web)
    pinned = secret_row(page, "ntfy topic")
    assert "set in .env" in pinned
    assert "<form" not in pinned  # .env wins: nothing to change here
    assert "not set" in secret_row(page, "Discord webhook URL")
    assert TOPIC not in page


def test_a_saved_secret_is_used_and_masked_from_then_on(web: Web) -> None:
    response = web.post("/settings/secrets/ntfy_topic", {"value": f"  {TOPIC} "}, htmx=False)
    assert response.status_code == 303
    page = text_of(web)
    assert "set here" in secret_row(page, "ntfy topic")
    assert TOPIC not in page
    # Anything that happens to contain it is masked too, without a restart.
    web.add(title=f"Backend Engineer {TOPIC}")
    assert TOPIC not in web.page("/")
    assert "Backend Engineer ***" in web.page("/")


def test_a_bad_secret_is_refused_without_echoing_it(web: Web) -> None:
    response = web.post("/settings/secrets/ntfy_topic", {"value": "short"}, htmx=False)
    assert response.status_code == 400
    assert "24–64 characters" in html.unescape(response.text)
    assert 'value="short"' not in response.text
    assert web.post("/settings/secrets/nope", {"value": TOPIC}).status_code == 400


def test_a_secret_pinned_in_env_cant_be_changed_here(make_web: Callable[..., Web]) -> None:
    web = make_web(store=SecretStore(Settings(_env_file=None, ntfy_topic=SecretStr(TOPIC))))
    response = web.post("/settings/secrets/ntfy_topic", {"value": OTHER_TOPIC}, htmx=False)
    assert response.status_code == 400
    assert ".env" in html.unescape(response.text)


def test_a_saved_secret_can_be_cleared(web: Web) -> None:
    web.post("/settings/secrets/ntfy_topic", {"value": TOPIC})
    assert web.post("/settings/secrets/ntfy_topic/clear", htmx=False).status_code == 303
    assert "not set" in secret_row(text_of(web), "ntfy topic")


def test_logs_mask_a_secret_saved_after_logging_started(make_web: Callable[..., Web]) -> None:
    registry = SecretRegistry()
    stream = StringIO()
    handler = configure_logging("INFO", secrets=registry, stream=stream)
    try:
        web = make_web(store=SecretStore(registry=registry))
        web.post("/settings/secrets/ntfy_topic", {"value": TOPIC})
        logging.getLogger("jobpulse.test").warning("posting to https://ntfy.sh/%s", TOPIC)
    finally:
        logging.getLogger().removeHandler(handler)
    assert TOPIC not in stream.getvalue()
    assert "https://ntfy.sh/***" in stream.getvalue()


# --- the test push -------------------------------------------------------------------------


def test_the_test_push_says_when_no_channel_is_set_up(web: Web) -> None:
    response = web.post("/settings/notify/test", htmx=False)
    assert response.status_code == 200
    assert "no notification channel is set up" in html.unescape(response.text)


def test_the_test_push_uses_a_topic_saved_here(
    make_web: Callable[..., Web], respx_mock: respx.MockRouter
) -> None:
    web = make_web(config=make_config(notify={"channels": ["ntfy"]}))
    web.post("/settings/secrets/ntfy_topic", {"value": TOPIC})
    ntfy = respx_mock.post(f"https://ntfy.sh/{TOPIC}").respond(200)
    response = web.post("/settings/notify/test", htmx=False)
    assert response.status_code == 200
    assert "Test push sent via ntfy." in html.unescape(response.text)
    assert ntfy.call_count == 1
    assert TOPIC not in response.text


def test_a_failed_test_push_is_reported(
    make_web: Callable[..., Web], respx_mock: respx.MockRouter
) -> None:
    web = make_web(config=make_config(notify={"channels": ["ntfy"]}))
    web.post("/settings/secrets/ntfy_topic", {"value": TOPIC})
    respx_mock.post(f"https://ntfy.sh/{TOPIC}").respond(403)
    response = web.post("/settings/notify/test", htmx=False)
    assert response.status_code == 502
    assert "The test push failed" in html.unescape(response.text)


# --- the resume ----------------------------------------------------------------------------


def test_an_uploaded_resume_is_stored_without_its_contact_lines(web: Web) -> None:
    response = upload(web, "resume.txt", RESUME)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings#resume"
    with web.sessions() as session:
        stored = resume_db.get(session)
    assert stored is not None
    assert stored.lines == (
        "Built Python services on Postgres for a payments team",
        "Led a React rewrite of the billing dashboard",
    )
    assert stored.rescored_at is None  # the worker embeds and re-scores next cycle
    page = text_of(web)
    assert "resume.txt, uploaded" in page
    assert "Led a React rewrite of the billing dashboard" in page
    assert "jane@example.com" not in page


@pytest.mark.parametrize(
    ("name", "data", "problem"),
    [
        ("resume.exe", b"MZ", "use a .pdf, .docx, .txt or .md file"),
        ("resume.txt", b"x" * (2 * 1024 * 1024 + 1), "larger than 2 MB"),
        ("resume.txt", b"Jane Doe\njane@example.com\n", "no content lines"),
        ("resume.txt", b"\xff\xfe\x00bad", "isn't UTF-8"),
    ],
)
def test_a_resume_that_cant_be_read_says_why(
    web: Web, name: str, data: bytes, problem: str
) -> None:
    response = upload(web, name, data)
    assert response.status_code == 400
    assert problem in html.unescape(response.text)
    with web.sessions() as session:
        assert resume_db.get(session) is None


def test_deleting_the_resume_puts_matches_back_on_stack_keywords(web: Web) -> None:
    upload(web, "resume.txt", RESUME)
    breakdown = {"freshness": 0.2, "title": 0.3, "stack": None, "resume": 0.2, "total": 0.7}
    posting = web.add(
        description_text="Python services.",
        score=0.7,
        score_breakdown=breakdown,
        resume_score=0.8,
        resume_line="Built Python services",
    )
    assert web.post("/settings/resume/clear", htmx=False).status_code == 303
    with web.sessions() as session:
        assert resume_db.get(session) is None
    row = web.row(posting)
    assert row.resume_score is None
    assert row.score_breakdown is not None
    assert "resume" not in row.score_breakdown
    assert row.score_breakdown["stack"] == pytest.approx(0.125)  # python, saturation 2


# --- the profile editor --------------------------------------------------------------------


def test_the_editor_shows_the_profile_as_yaml(web: Web) -> None:
    page = text_of(web)
    assert f'name="revision" value="{revision(web)}"' in page
    assert "contact_email: dev@example.com" in page
    assert "instant_threshold: 0.5" in page
    assert "freshness_hours" not in page  # infrastructure stays in config.yaml


def editor_text(web: Web) -> str:
    found = re.search(
        r'<textarea name="profile"[^>]*>(.*?)</textarea>', web.page("/settings"), re.S
    )
    assert found is not None
    return html.unescape(found.group(1))


def test_a_saved_profile_applies_without_a_restart(web: Web) -> None:
    before = revision(web)
    posting = web.add(posted_at=NOW - timedelta(hours=2))
    assert "2026-10-23 12:00 UTC" in web.page("/")  # the card's time, in the profile's zone
    edited = editor_text(web).replace("timezone: UTC", "timezone: America/New_York")
    response = web.post(
        "/settings/profile", {"profile": edited, "revision": str(before)}, htmx=False
    )
    assert response.status_code == 303
    assert revision(web) == before + 1
    assert "timezone: America/New_York" in editor_text(web)
    assert "2026-10-23 08:00 EDT" in web.page("/")
    assert web.row(posting).status == "New"


def test_errors_are_listed_by_path_and_the_text_is_kept(web: Web) -> None:
    before = revision(web)
    edited = "timezone: Mars/Base\nfilters:\n  title:\n    include: ['(']\n"
    response = web.post("/settings/profile", {"profile": edited, "revision": str(before)})
    assert response.status_code == 400
    page = html.unescape(response.text)
    assert "<code>timezone</code>" in page
    assert "<code>filters.title.include[0].pattern</code>" in page
    assert "Mars/Base" in page  # the textarea keeps what was sent
    assert revision(web) == before


def test_a_stale_save_answers_409_and_keeps_the_text(web: Web) -> None:
    before = revision(web)
    response = web.post(
        "/settings/profile", {"profile": "timezone: Europe/Paris\n", "revision": str(before - 1)}
    )
    assert response.status_code == 409
    page = html.unescape(response.text)
    assert "changed since this page was loaded" in page
    assert "timezone: Europe/Paris" in page
    assert revision(web) == before


def test_a_new_install_can_save_its_first_profile_here(web: Web) -> None:
    with web.sessions() as session:
        session.query(ProfileRow).delete()
        session.commit()
    page = text_of(web, "/")
    assert "No profile yet, so the worker waits" in page
    assert 'name="revision" value="0"' in text_of(web)
    response = web.post(
        "/settings/profile", {"profile": "contact_email: me@example.com\n", "revision": "0"}
    )
    assert response.status_code == 200  # HTMX: HX-Redirect back to Settings
    assert response.headers["HX-Redirect"] == "/settings#profile"
    assert "No profile yet" not in text_of(web, "/")


def test_an_invalid_stored_profile_shows_a_banner(web: Web) -> None:
    with web.sessions() as session:
        session.execute(
            update(ProfileRow).values(
                data={"timezone": "Mars/Base"}, revision=ProfileRow.revision + 1
            )
        )
        session.commit()
    page = text_of(web, "/")
    assert "the stored profile is invalid" in page
    assert "fix it on Settings" in page
