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

JobSpy doesn't go through httpx, so its results come from a ``jobspy`` section::

    jobspy:
      - site: linkedin
        search_term: backend engineer
        file: jobspy/linkedin_backend.json   # {"rows": [...], "errors": {}, "warnings": {}}

A search with no matching entry raises, which the adapter records as a failed run.

An ``infra`` section overrides how the worker runs for the recording, on top of the stored
settings (``fixture_infra``), for what Settings can't set::

    infra:
      linkedin: {delay_min_seconds: 0, delay_max_seconds: 0}   # nothing to be polite to
      discovery: {max_page_fetches_per_cycle: 0}
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import yaml

from jobpulse.config import InfraConfig

_CONTENT_TYPES = {".json": "application/json", ".html": "text/html", ".txt": "text/plain"}


def _load_routes(directory: Path) -> dict[str, Any]:
    spec = yaml.safe_load((directory / "routes.yaml").read_text(encoding="utf-8")) or {}
    return spec if isinstance(spec, dict) else {}


def fixture_infra(directory: Path, infra: InfraConfig) -> InfraConfig:
    """``infra`` with the recording's ``infra`` overrides (nested sections merge)."""
    overrides = _load_routes(directory).get("infra") or {}
    data = infra.model_dump()
    for section, values in overrides.items():
        if isinstance(values, dict) and isinstance(data.get(section), dict):
            data[section] = {**data[section], **values}
        else:
            data[section] = values
    return InfraConfig.model_validate(data)


class FixtureScraper:
    """A stand-in for ``jobspy.scrape_jobs`` that answers from recorded rows."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._entries = list(_load_routes(directory).get("jobspy") or [])

    def __call__(self, **arguments: Any) -> Any:
        import pandas as pd
        from jobspy.model import JobPost

        sites = arguments.get("site_name") or []
        site = sites[0] if isinstance(sites, list) and sites else str(sites)
        term = arguments.get("search_term")
        entry = next(
            (e for e in self._entries if e.get("site") == site and e.get("search_term") == term),
            None,
        )
        if entry is None:
            raise LookupError(f"no JobSpy fixture for {site} search {term!r}")
        data = json.loads((self._directory / entry["file"]).read_text(encoding="utf-8"))
        rows = []
        for recorded in data.get("rows") or []:
            row = dict(recorded)
            if isinstance(row.get("date_posted"), str):
                row["date_posted"] = date.fromisoformat(row["date_posted"])
            rows.append(row)
        # The same shape scrape_jobs returns: every JobPost column, errors and warnings in attrs.
        frame = pd.DataFrame(rows, columns=list(JobPost.model_fields))
        frame.attrs = {
            "errors": dict(data.get("errors") or {}),
            "warnings": {site: list(items) for site, items in (data.get("warnings") or {}).items()},
        }
        return frame


class FixtureTransport(httpx.AsyncBaseTransport):
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._routes: dict[tuple[str, str], dict[str, Any]] = {
            (str(route.get("method", "GET")).upper(), str(route["url"])): route
            for route in _load_routes(directory).get("routes") or []
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
