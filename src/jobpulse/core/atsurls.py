"""Recognize Greenhouse, Lever, Ashby and Workday board and job URLs, and find them in HTML.

``detect_ats_url`` knows the boards discovery may add on its own (Greenhouse, Lever, Ashby);
``detect_workday_url`` knows Workday's, which only you add (config or the dashboard).
"""

import html
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import SplitResult, parse_qs, unquote, urlsplit

from jobpulse.core.models import Region


@dataclass(frozen=True, slots=True)
class AtsRef:
    ats: Literal["greenhouse", "lever", "ashby", "workday"]
    token: str
    job_id: str | None = None
    region: Region = "us"


@dataclass(frozen=True, slots=True)
class WorkdayBoard:
    """One Workday careers site: ``https://{tenant}.{host}.myworkdayjobs.com/{site}``.

    Its token is ``{tenant}.{host}/{site}``, e.g. ``nvidia.wd5/NVIDIAExternalCareerSite``.
    """

    tenant: str
    host: str  # the data center, "wd1", "wd5", "wd12", ...
    site: str

    @property
    def token(self) -> str:
        return f"{self.tenant}.{self.host}/{self.site}"

    @property
    def origin(self) -> str:
        return f"https://{self.tenant}.{self.host}.myworkdayjobs.com"

    @property
    def board_url(self) -> str:
        return f"{self.origin}/{self.site}"

    @property
    def api_url(self) -> str:
        """The CXS API base; ``/jobs`` lists, ``{externalPath}`` reads one job."""
        return f"{self.origin}/wday/cxs/{self.tenant}/{self.site}"


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
    r"(?:https?:)?//(?:[a-z0-9-]+\.)*"
    r"(?:greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|myworkdaysite\.com)"
    r"(?:/[^\s\"'<>]*)?",
    re.IGNORECASE,
)
# {tenant}.wdN.myworkdayjobs.com, and wdN.myworkdaysite.com/recruiting/{tenant}/{site}
_WORKDAY_JOBS_HOST = re.compile(
    r"(?P<tenant>[a-z0-9][a-z0-9-]*)\.(?P<host>wd\d{1,3})\.myworkdayjobs\.com"
)
_WORKDAY_SITE_HOST = re.compile(r"(?P<host>wd\d{1,3})\.myworkdaysite\.com")
_WORKDAY_TOKEN = re.compile(
    r"(?P<tenant>[a-z0-9][a-z0-9-]*)\.(?P<host>wd\d{1,3})/(?P<site>[a-z0-9][a-z0-9_.-]*)",
    re.IGNORECASE,
)
_WORKDAY_TENANT = re.compile(r"[a-z0-9][a-z0-9-]*", re.IGNORECASE)
_WORKDAY_SITE = re.compile(r"[a-z0-9][a-z0-9_.-]*", re.IGNORECASE)
_WORKDAY_RESERVED = frozenset({"wday"})
_LOCALE = re.compile(r"[a-z]{2}-[a-z]{2}", re.IGNORECASE)


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


def workday_board(token: str) -> WorkdayBoard | None:
    """The board a ``{tenant}.{host}/{site}`` token names, or ``None``."""
    match = _WORKDAY_TOKEN.fullmatch(token.strip())
    if match is None:
        return None
    return WorkdayBoard(match["tenant"].lower(), match["host"].lower(), match["site"])


def _workday_job(segments: list[str]) -> str | None:
    """The job id in what follows the site: ``job/{location}/{id}``, ``job/{id}`` or
    ``details/{id}`` (anything after the id, such as ``/apply``, is ignored)."""
    if len(segments) < 2:
        return None
    kind = segments[0].lower()
    if kind == "job":
        return segments[2] if len(segments) >= 3 else segments[1]
    if kind == "details":
        return segments[1]
    return None


def _without_locale(segments: list[str]) -> list[str]:
    return segments[1:] if segments and _LOCALE.fullmatch(segments[0]) else segments


def detect_workday_url(url: str) -> AtsRef | None:
    """The Workday board (and job, if present) a URL points at, or ``None``: every
    ``{tenant}.wdN.myworkdayjobs.com`` form (with or without a locale such as ``en-US``),
    ``wdN.myworkdaysite.com/recruiting/{tenant}/{site}``, and the CXS API's own URLs."""
    parts = _split(url)
    if parts is None or parts.hostname is None:
        return None
    host = parts.hostname.lower()
    segments = [unquote(segment) for segment in parts.path.split("/") if segment]
    jobs_host = _WORKDAY_JOBS_HOST.fullmatch(host)
    host_match = jobs_host or _WORKDAY_SITE_HOST.fullmatch(host)
    if host_match is None:
        return None
    host_number = host_match["host"]
    if [segment.lower() for segment in segments[:2]] == ["wday", "cxs"]:
        if len(segments) < 4:
            return None
        tenant = jobs_host["tenant"] if jobs_host else segments[2]
        site, rest = segments[3], segments[4:]
    elif jobs_host is not None:
        segments = _without_locale(segments)
        if not segments:
            return None
        tenant, site, rest = jobs_host["tenant"], segments[0], segments[1:]
    else:
        segments = _without_locale(segments)
        if len(segments) < 3 or segments[0].lower() != "recruiting":
            return None
        tenant, site, rest = segments[1], segments[2], segments[3:]
    if (
        not _WORKDAY_TENANT.fullmatch(tenant)
        or not _WORKDAY_SITE.fullmatch(site)
        or site.lower() in _WORKDAY_RESERVED
    ):
        return None
    board = WorkdayBoard(tenant.lower(), host_number, site)
    return AtsRef("workday", board.token, _workday_job(rest))


def normalize_workday_token(value: str) -> str | None:
    """A Workday token from ``{tenant}.{host}/{site}`` or any board or job URL; ``None`` if
    it's neither. The tenant and host are lowercased; the site keeps its case."""
    board = workday_board(value)
    if board is not None:
        return board.token
    ref = detect_workday_url(value.strip())
    return ref.token if ref is not None else None


def find_ats_refs_in_html(page: str) -> list[AtsRef]:
    """Every distinct ATS board linked or embedded in a page, in order of appearance."""
    refs: list[AtsRef] = []
    seen: set[tuple[str, str, str]] = set()
    for match in _ATS_URL_IN_HTML.finditer(html.unescape(page)):
        url = match.group(0)
        if url.startswith("//"):
            url = "https:" + url
        ref = detect_ats_url(url) or detect_workday_url(url)
        if ref is None:
            continue
        key = (ref.ats, ref.token.lower(), ref.region)
        if key not in seen:
            seen.add(key)
            refs.append(AtsRef(ref.ats, ref.token, None, ref.region))
    return refs


def board_url(ats: str, token: str, region: str = "us") -> str | None:
    """The public job board a watchlist company's token points at."""
    eu = region == "eu"
    if ats == "greenhouse":
        return f"https://job-boards.{'eu.' if eu else ''}greenhouse.io/{token}"
    if ats == "lever":
        return f"https://jobs.{'eu.' if eu else ''}lever.co/{token}"
    if ats == "ashby":
        return f"https://{_ASHBY_HOST}/{token}"
    if ats == "workday" and (board := workday_board(token)) is not None:
        return board.board_url
    return None


def gh_jid(url: str) -> str | None:
    """The numeric ``gh_jid`` query parameter, if any."""
    parts = _split(url)
    if parts is None:
        return None
    value = _first(parse_qs(parts.query), "gh_jid")
    return value if value and value.isdigit() else None
