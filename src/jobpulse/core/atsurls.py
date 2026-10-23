"""Recognize Greenhouse, Lever and Ashby board and job URLs, and find them in HTML."""

import html
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import SplitResult, parse_qs, unquote, urlsplit

from jobpulse.core.models import Region


@dataclass(frozen=True, slots=True)
class AtsRef:
    ats: Literal["greenhouse", "lever", "ashby"]
    token: str
    job_id: str | None = None
    region: Region = "us"


_GREENHOUSE_HOSTS: dict[str, Region] = {
    "boards.greenhouse.io": "us",
    "job-boards.greenhouse.io": "us",
    "boards.eu.greenhouse.io": "eu",
    "job-boards.eu.greenhouse.io": "eu",
}
_GREENHOUSE_API_HOST = "boards-api.greenhouse.io"
_LEVER_HOSTS: dict[str, Region] = {"jobs.lever.co": "us", "jobs.eu.lever.co": "eu"}
_ASHBY_HOST = "jobs.ashbyhq.com"
_ASHBY_RESERVED = frozenset({"api", "embed"})
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_ATS_URL_IN_HTML = re.compile(
    r"(?:https?:)?//(?:[a-z0-9-]+\.)*(?:greenhouse\.io|lever\.co|ashbyhq\.com)(?:/[^\s\"'<>]*)?",
    re.IGNORECASE,
)


def _split(url: str) -> SplitResult | None:
    text = url.strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text.lstrip("/")
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    return parts


def _first(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values else None


def _greenhouse(segments: list[str], query: dict[str, list[str]], region: Region) -> AtsRef | None:
    if not segments:
        return None
    if segments[0] == "embed":
        token = _first(query, "for")
        if not token or not _TOKEN.fullmatch(token):
            return None
        job = _first(query, "token") if segments[1:2] == ["job_app"] else None
        return AtsRef("greenhouse", token.lower(), job if job and job.isdigit() else None, region)
    token = segments[0]
    if not _TOKEN.fullmatch(token):
        return None
    job_id: str | None = None
    if len(segments) >= 3 and segments[1] == "jobs" and segments[2].isdigit():
        job_id = segments[2]
    elif (jid := _first(query, "gh_jid")) and jid.isdigit():
        job_id = jid
    return AtsRef("greenhouse", token.lower(), job_id, region)


def _greenhouse_api(segments: list[str]) -> AtsRef | None:
    if len(segments) < 3 or segments[:2] != ["v1", "boards"] or not _TOKEN.fullmatch(segments[2]):
        return None
    job_id = segments[4] if segments[3:4] == ["jobs"] and len(segments) > 4 else None
    return AtsRef(
        "greenhouse", segments[2].lower(), job_id if job_id and job_id.isdigit() else None
    )


def _uuid_segment(segments: list[str]) -> str | None:
    if len(segments) > 1 and _UUID.fullmatch(segments[1]):
        return segments[1].lower()
    return None


def detect_ats_url(url: str) -> AtsRef | None:
    """The ATS board (and job, if present) a URL points at, or ``None``."""
    parts = _split(url)
    if parts is None or parts.hostname is None:
        return None
    host = parts.hostname.lower().removeprefix("www.")
    segments = [unquote(segment) for segment in parts.path.split("/") if segment]
    query = parse_qs(parts.query)
    if host in _GREENHOUSE_HOSTS:
        return _greenhouse(segments, query, _GREENHOUSE_HOSTS[host])
    if host == _GREENHOUSE_API_HOST:
        return _greenhouse_api(segments)
    if host in _LEVER_HOSTS:
        if not segments or not _TOKEN.fullmatch(segments[0]):
            return None
        return AtsRef("lever", segments[0].lower(), _uuid_segment(segments), _LEVER_HOSTS[host])
    if host == _ASHBY_HOST:
        if (
            not segments
            or segments[0].lower() in _ASHBY_RESERVED
            or not _TOKEN.fullmatch(segments[0])
        ):
            return None
        # Ashby board names keep their case for API calls; ATS keys lowercase them.
        return AtsRef("ashby", segments[0], _uuid_segment(segments))
    return None


def find_ats_refs_in_html(page: str) -> list[AtsRef]:
    """Every distinct ATS board linked or embedded in a page, in order of appearance."""
    refs: list[AtsRef] = []
    seen: set[tuple[str, str, str]] = set()
    for match in _ATS_URL_IN_HTML.finditer(html.unescape(page)):
        url = match.group(0)
        if url.startswith("//"):
            url = "https:" + url
        ref = detect_ats_url(url)
        if ref is None:
            continue
        key = (ref.ats, ref.token.lower(), ref.region)
        if key not in seen:
            seen.add(key)
            refs.append(AtsRef(ref.ats, ref.token, None, ref.region))
    return refs


def gh_jid(url: str) -> str | None:
    """The numeric ``gh_jid`` query parameter, if any."""
    parts = _split(url)
    if parts is None:
        return None
    value = _first(parse_qs(parts.query), "gh_jid")
    return value if value and value.isdigit() else None
