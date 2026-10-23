"""Stable ids for job-board postings (detection rule 1), parsed from the job URL rather than
from JobSpy's own ``id`` column: LinkedIn's numeric id in ``/jobs/view/{id}`` and Indeed's
``jk`` query parameter."""

import re
from urllib.parse import parse_qs, urlsplit

from jobpulse.core.models import SearchSite

# /jobs/view/4101234567 or /jobs/view/backend-engineer-at-acme-4101234567
_LINKEDIN_VIEW = re.compile(r"/jobs/view/(?:[^/]*-)?(?P<id>\d{5,})/?")
_INDEED_KEY = re.compile(r"[0-9A-Za-z]{6,32}")


def _host(url: str) -> tuple[str, str, str] | None:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    return parts.hostname.lower(), parts.path, parts.query


def linkedin_job_id(url: str) -> str | None:
    parts = _host(url)
    if parts is None:
        return None
    host, path, _ = parts
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    match = _LINKEDIN_VIEW.fullmatch(path)
    return match["id"] if match else None


def indeed_job_key(url: str) -> str | None:
    parts = _host(url)
    if parts is None:
        return None
    host, _, query = parts
    if host != "indeed.com" and not host.endswith(".indeed.com"):  # www., uk., ca., ...
        return None
    values = parse_qs(query).get("jk")
    key = values[0] if values else ""
    return key if _INDEED_KEY.fullmatch(key) else None


def board_job_id(site: SearchSite, url: str) -> str | None:
    return linkedin_job_id(url) if site == "linkedin" else indeed_job_key(url)
