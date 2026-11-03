"""Cross-source dedupe (detection rule 5): canonical URLs, ATS keys and fingerprints."""

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobpulse.core.atsurls import detect_ats_url, detect_workday_url
from jobpulse.core.locations import location_key
from jobpulse.core.models import Posting
from jobpulse.core.text import collapse_whitespace

TRACKING_PARAMS = frozenset(
    {"gh_src", "source", "ref", "src", "lever-source", "lever-origin", "trk", "refid", "trackingid"}
)
TRACKING_PREFIXES = ("utm_",)
LEGAL_SUFFIXES = frozenset({"inc", "llc", "ltd", "corp", "corporation", "incorporated", "limited"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_TRAILING_ACTION = re.compile(r"/(?:apply|application)$", re.IGNORECASE)
_PUNCTUATION = re.compile(r"[^\w\s]")


def ats_key(ats: str, token: str, job_id: str) -> str:
    """One canonical form per ATS: ``greenhouse:{token}:{job_id}`` and so on."""
    return f"{ats}:{token.lower()}:{job_id.lower()}"


def _is_tracking(name: str) -> bool:
    key = name.casefold().removesuffix("[]")
    return key in TRACKING_PARAMS or key.startswith(TRACKING_PREFIXES)


def canonicalize_url(url: str, *, greenhouse_hosts: Mapping[str, str] | None = None) -> str:
    """Rule 5a. Returns an ATS key for Greenhouse, Lever, Ashby and Workday job URLs (including
    ``?gh_jid=`` on a custom domain whose Greenhouse token is known), else a cleaned URL:
    lowercase scheme and host, no fragment, no tracking parameters, no trailing ``/apply``,
    ``/application`` or slash, remaining parameters sorted."""
    text = url.strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text.lstrip("/")
    parts = urlsplit(text)
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host if port is None or port == _DEFAULT_PORTS.get(scheme) else f"{host}:{port}"
    path = _TRAILING_ACTION.sub("", parts.path.rstrip("/")).rstrip("/")
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)
    ]

    cleaned = urlunsplit((scheme, netloc, path, urlencode(query), ""))
    ref = detect_ats_url(cleaned) or detect_workday_url(cleaned)
    if ref is not None and ref.job_id is not None:
        return ats_key(ref.ats, ref.token, ref.job_id)
    if greenhouse_hosts:
        token = greenhouse_hosts.get(host.removeprefix("www."))
        job_id = dict(query).get("gh_jid")
        if token and job_id and job_id.isdigit():
            return ats_key("greenhouse", token, job_id)
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


def _words(text: str) -> list[str]:
    return collapse_whitespace(_PUNCTUATION.sub(" ", text.casefold())).split()


def normalize_company(name: str) -> str:
    """Casefold, drop punctuation and trailing legal suffixes (Inc, LLC, Ltd, Corp)."""
    words = _words(name)
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def normalize_title(title: str) -> str:
    """Casefold and drop punctuation. Seniority words stay in."""
    return " ".join(_words(title))


def fingerprint(company: str, title: str, primary_location: str | None) -> str:
    """sha256 of normalized company, title and city-level primary location (rule 5c)."""
    key = f"{normalize_company(company)}|{normalize_title(title)}|{location_key(primary_location)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def posting_fingerprint(posting: Posting) -> str:
    if not normalize_company(posting.company_name):
        # Without a company the fingerprint would match unrelated jobs with the same title
        # and city, so it matches only this posting.
        key = f"no company|{posting.source}|{posting.external_id}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()
    primary = posting.locations[0] if posting.locations else None
    return fingerprint(posting.company_name, posting.title, primary)


def merge_also_seen(
    existing: Sequence[Mapping[str, Any]], entry: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """A new list with ``entry`` appended unless that (source, external_id) is already in."""
    merged = [dict(item) for item in existing]
    key = (entry.get("source"), entry.get("external_id"))
    if all((item.get("source"), item.get("external_id")) != key for item in merged):
        merged.append(dict(entry))
    return merged
