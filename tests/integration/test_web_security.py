"""Dashboard security: basic auth on and off, the CSRF token on every POST, response headers,
and secrets never rendered."""

import base64
import re
from collections.abc import Callable
from datetime import timedelta

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute

from jobpulse.db.tables import Run
from jobpulse.web import routes
from tests.factories import NOW
from tests.integration.conftest import Web

USER, PASSWORD = "me", "s3cret-pass-29"
TOPIC = "jobpulse-Zq3vX9kR2mT7wL4pB8nC1dF6"


def basic(user: str, password: str) -> dict[str, str]:
    encoded = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


# --- basic auth -------------------------------------------------------------------------


def test_without_credentials_the_dashboard_is_open(web: Web) -> None:
    for path in ("/", "/?view=saved", "/health", "/watchlist", "/static/htmx.min.js"):
        assert web.client.get(path).status_code == 200, path


def test_basic_auth_guards_every_page_and_file(make_web: Callable[..., Web]) -> None:
    client = make_web(credentials=(USER, PASSWORD)).client

    for path in ("/", "/health", "/watchlist", "/static/htmx.min.js", "/static/app.css"):
        refused = client.get(path)
        assert refused.status_code == 401, path
        assert refused.headers["www-authenticate"].startswith('Basic realm="jobpulse"')
        assert client.get(path, headers=basic(USER, PASSWORD)).status_code == 200, path

    for headers in (
        basic(USER, "wrong"),
        basic("someone", PASSWORD),
        basic(USER, PASSWORD + "x"),
        {"Authorization": "Basic not-base64!!"},
        {"Authorization": "Bearer " + PASSWORD},
        {"Authorization": "Basic " + base64.b64encode(b"no-colon").decode()},
    ):
        assert client.get("/", headers=headers).status_code == 401, headers


def test_the_liveness_probe_needs_no_password(make_web: Callable[..., Web]) -> None:
    client = make_web(credentials=(USER, PASSWORD)).client
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


def test_posts_need_the_password_too(make_web: Callable[..., Web]) -> None:
    web = make_web(credentials=(USER, PASSWORD))
    posting = web.add(title="Backend Engineer")
    response = web.client.post(f"/postings/{posting}/status", data={"status": "Saved"})
    assert response.status_code == 401
    assert web.row(posting).status == "New"


def test_the_password_is_never_rendered(make_web: Callable[..., Web]) -> None:
    web = make_web(credentials=(USER, PASSWORD), secrets=[PASSWORD])
    for path in ("/", "/health", "/watchlist"):
        response = web.client.get(path, headers=basic(USER, PASSWORD))
        assert PASSWORD not in response.text


# --- CSRF -----------------------------------------------------------------------------------


def test_the_first_visit_sets_a_strict_http_only_token_cookie(web: Web) -> None:
    response = web.client.get("/")
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("jobpulse_csrf=")
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Path=/" in cookie
    token = web.client.cookies["jobpulse_csrf"]
    assert len(token) >= 32
    assert token in response.text  # for HTMX's header and the plain forms' hidden field
    again = web.client.get("/")
    assert "set-cookie" not in again.headers  # the cookie is kept, not rotated per page


def test_posts_without_a_matching_token_are_refused(web: Web) -> None:
    posting = web.add(title="Backend Engineer")
    url = f"/postings/{posting}/status"
    token = web.token

    assert web.client.post(url, data={"status": "Saved"}).status_code == 403
    assert (
        web.client.post(
            url, data={"status": "Saved"}, headers={"X-CSRF-Token": "x" * 43}
        ).status_code
        == 403
    )
    assert (
        web.client.post(
            url,
            data={
                "status": "Saved",
                "csrf_token": token[:-1] + ("B" if token.endswith("A") else "A"),
            },
        ).status_code
        == 403
    )
    assert web.row(posting).status == "New"

    assert (
        web.client.post(
            url, data={"status": "Saved"}, headers={"X-CSRF-Token": token, "HX-Request": "true"}
        ).status_code
        == 200
    )
    assert web.row(posting).status == "Saved"
    plain = web.client.post(
        url, data={"status": "Applied", "csrf_token": token}, follow_redirects=False
    )
    assert plain.status_code == 303
    assert web.row(posting).status == "Applied"


def test_a_token_without_the_cookie_is_refused(web: Web) -> None:
    posting = web.add(title="Backend Engineer")
    token = web.token
    web.client.cookies.clear()
    response = web.client.post(
        f"/postings/{posting}/status", data={"status": "Saved"}, headers={"X-CSRF-Token": token}
    )
    assert response.status_code == 403


def test_cross_site_posts_are_refused_even_with_the_token(web: Web) -> None:
    posting = web.add(title="Backend Engineer")
    response = web.client.post(
        f"/postings/{posting}/status",
        data={"status": "Saved"},
        headers={"X-CSRF-Token": web.token, "Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
    assert web.row(posting).status == "New"


def test_every_post_route_checks_the_token(web: Web) -> None:
    # POST routes live on the actions router, whose dependency checks the token; the pages
    # router has none. (FastAPI 0.150 keeps included routers whole, so ask the routers.)
    def posts(router: APIRouter) -> list[str]:
        return sorted(
            route.path
            for route in router.routes
            if isinstance(route, APIRoute) and "POST" in route.methods
        )

    assert posts(routes.pages) == []
    paths = posts(routes.actions)
    assert paths == [
        "/companies/block",
        "/companies/unblock",
        "/postings/bulk-status",
        "/postings/{posting_id}/status",
        "/watchlist/add",
        "/watchlist/detect",
        "/watchlist/{company_id}/active",
        "/watchlist/{company_id}/priority",
    ]
    assert web.token  # the cookie is set; the requests below just don't send the token
    for path in paths:
        concrete = re.sub(r"\{[a-z_]+\}", "1", path)
        assert web.client.post(concrete, data={}).status_code == 403, path


# --- headers and rendering -----------------------------------------------------------------


def test_pages_carry_security_headers(web: Web) -> None:
    response = web.client.get("/")
    policy = response.headers["content-security-policy"]
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "base-uri 'none'",
    ):
        assert directive in policy
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    script = web.client.get("/static/htmx.min.js")
    assert script.headers["x-content-type-options"] == "nosniff"
    assert "javascript" in script.headers["content-type"]


def test_there_are_no_api_docs(web: Web) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert web.client.get(path).status_code == 404, path


def test_secrets_are_masked_in_every_rendered_value(make_web: Callable[..., Web]) -> None:
    web = make_web(secrets=[TOPIC])
    web.add(title=f"Backend Engineer {TOPIC}")
    with web.sessions() as session:
        session.add(
            Run(
                source="greenhouse",
                target="acme",
                started_at=NOW - timedelta(minutes=5),
                finished_at=NOW - timedelta(minutes=5),
                ok=False,
                error=f"POST https://ntfy.sh/{TOPIC} failed",
                error_kind="other",
            )
        )
        session.commit()

    for path in ("/", "/health"):
        page = web.page(path)
        assert TOPIC not in page
        assert "***" in page


def test_templates_escape_what_sources_send(web: Web) -> None:
    posting = web.add(title='<script>alert("x")</script> Engineer', company_name="A&B <Corp>")
    page = web.page("/")
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page
    assert "A&amp;B &lt;Corp&gt;" in page
    assert posting in [int(x) for x in re.findall(r'id="posting-(\d+)"', page)]


@pytest.mark.parametrize("path", ["/", "/health", "/watchlist"])
def test_pages_load_scripts_and_styles_from_this_server_only(web: Web, path: str) -> None:
    page = web.page(path)
    assert re.findall(r"<script[^>]*>", page) == [
        '<script src="/static/htmx.min.js" defer>',
        '<script src="/static/app.js" defer>',  # the blocked-company listener, no inline code
    ]
    stylesheets = re.findall(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"', page)
    assert stylesheets == ["/static/app.css"]
    assert "<style" not in page  # the CSP allows no inline styles


def test_a_client_on_another_origin_gets_no_cors_headers(web: Web) -> None:
    response = web.client.get("/", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


def test_unknown_paths_get_a_plain_404(web: Web) -> None:
    response = web.client.get("/no-such-page")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/", "text/html"),
        ("/?view=saved", "text/html"),
        ("/health", "text/html"),
        ("/watchlist", "text/html"),
        ("/healthz", "text/plain"),
        ("/static/htmx.min.js", "text/javascript"),
    ],
)
def test_pages_answer_head_like_get(web: Web, path: str, content_type: str) -> None:
    response = web.client.head(path)
    assert response.status_code == 200, path
    assert response.headers["content-type"].startswith(content_type)
    assert response.headers["x-content-type-options"] == "nosniff"


def test_head_requests_need_the_password_too(make_web: Callable[..., Web]) -> None:
    client = make_web(credentials=(USER, PASSWORD)).client
    assert client.head("/").status_code == 401
    assert client.head("/", headers=basic(USER, PASSWORD)).status_code == 200
    assert client.head("/healthz").status_code == 200
