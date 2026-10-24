"""Dashboard security: basic auth, the CSRF token, and response headers.

- Basic auth is on when both ``DASHBOARD_USER`` and ``DASHBOARD_PASSWORD`` are set. Every path
  needs it except ``/healthz``, which says only "ok". Credentials are compared in constant
  time.
- CSRF: each browser gets a random token in a ``SameSite=Strict``, ``HttpOnly`` cookie. Pages
  render the same token into HTMX's ``X-CSRF-Token`` header and the plain forms' hidden field,
  and every POST must send it back. Cross-site POSTs are refused outright.
- Every response carries a strict Content-Security-Policy (scripts and styles from this server
  only) and the usual hardening headers.
"""

import base64
import binascii
import hashlib
import hmac
import re
import secrets

from fastapi import HTTPException, Request
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSRF_COOKIE = "jobpulse_csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"
CSRF_MAX_AGE = 365 * 24 * 3600
AUTH_EXEMPT_PATHS = frozenset({"/healthz"})
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    )
)
SECURITY_HEADERS = {
    "content-security-policy": CONTENT_SECURITY_POLICY,
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
}


def _digest(value: bytes) -> bytes:
    # Equal-length inputs for compare_digest, so not even the length leaks.
    return hashlib.sha256(value).digest()


def same_secret(given: str | bytes, expected: str | bytes) -> bool:
    """Constant-time comparison of two strings or byte strings."""
    left = given.encode("utf-8") if isinstance(given, str) else given
    right = expected.encode("utf-8") if isinstance(expected, str) else expected
    return hmac.compare_digest(_digest(left), _digest(right))


class BasicAuthMiddleware:
    """HTTP basic auth on every path but ``/healthz``."""

    def __init__(self, app: ASGIApp, *, username: str, password: str) -> None:
        self.app = app
        self._username = username
        self._password = password

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in AUTH_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        if self._authorized(Headers(scope=scope).get("authorization")):
            await self.app(scope, receive, send)
            return
        response = PlainTextResponse(
            "authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="jobpulse", charset="UTF-8"'},
        )
        await response(scope, receive, send)

    def _authorized(self, header: str | None) -> bool:
        if not header:
            return False
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            decoded = base64.b64decode(encoded.strip(), validate=True)
        except (binascii.Error, ValueError):
            return False
        user, colon, password = decoded.partition(b":")
        if not colon:
            return False
        # Both comparisons always run, so timing says nothing about which part was wrong.
        user_ok = same_secret(user, self._username)
        password_ok = same_secret(password, self._password)
        return user_ok and password_ok


class CsrfCookieMiddleware:
    """Gives every browser a CSRF token cookie and exposes the token as
    ``request.state.csrf_token`` for the templates."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = Request(scope).cookies.get(CSRF_COOKIE, "")
        fresh = not _TOKEN.fullmatch(token)
        if fresh:
            token = secrets.token_urlsafe(32)
        scope.setdefault("state", {})["csrf_token"] = token
        if not fresh:
            await self.app(scope, receive, send)
            return
        secure = "; Secure" if scope.get("scheme") == "https" else ""
        cookie = (
            f"{CSRF_COOKIE}={token}; Path=/; Max-Age={CSRF_MAX_AGE}; HttpOnly; "
            f"SameSite=Strict{secure}"
        )

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append("set-cookie", cookie)
            await send(message)

        await self.app(scope, receive, send_with_cookie)


class SecurityHeadersMiddleware:
    """CSP and hardening headers on every response; ``no-store`` on everything but static
    files, so a shared or remote browser keeps no copy of your job list."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        static = scope["path"].startswith("/static/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
                if not static:
                    headers["cache-control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def verify_csrf(request: Request) -> None:
    """Dependency for every POST: the token from the header or the form field must match the
    cookie, and the request must not come from another site."""
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="cross-site request refused")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    sent = request.headers.get(CSRF_HEADER)
    if sent is None:
        form = await request.form()
        value = form.get(CSRF_FIELD)
        sent = value if isinstance(value, str) else ""
    if not cookie or not sent or not same_secret(sent, cookie):
        raise HTTPException(
            status_code=403, detail="missing or stale form token; reload the page and try again"
        )
