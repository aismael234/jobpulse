"""Serve recorded HTTP responses for ``jobpulse poll --fixtures DIR`` (development only).

``DIR/routes.yaml`` lists the responses::

    routes:
      - url: https://boards-api.greenhouse.io/v1/boards/acme/jobs
        file: greenhouse/acme_jobs.json   # relative to DIR
      - url: https://api.lever.co/v0/postings/beta?mode=json
        status: 503
        body: Service Unavailable

``method`` defaults to GET and ``status`` to 200. A request with no matching route fails
like a network error, so fixture mode never reaches the real network.
"""

from pathlib import Path
from typing import Any

import httpx
import yaml

_CONTENT_TYPES = {".json": "application/json", ".html": "text/html", ".txt": "text/plain"}


class FixtureTransport(httpx.AsyncBaseTransport):
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        spec = yaml.safe_load((directory / "routes.yaml").read_text(encoding="utf-8")) or {}
        self._routes: dict[tuple[str, str], dict[str, Any]] = {
            (str(route.get("method", "GET")).upper(), str(route["url"])): route
            for route in spec.get("routes", [])
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        route = self._routes.get((request.method, str(request.url)))
        if route is None:
            raise httpx.ConnectError(
                f"no fixture for {request.method} {request.url}", request=request
            )
        headers = dict(route.get("headers") or {})
        if "file" in route:
            path = self._directory / route["file"]
            content = path.read_bytes()
            headers.setdefault(
                "Content-Type", _CONTENT_TYPES.get(path.suffix, "application/octet-stream")
            )
        else:
            content = str(route.get("body", "")).encode("utf-8")
        return httpx.Response(
            int(route.get("status", 200)), headers=headers, content=content, request=request
        )
