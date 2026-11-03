"""Ashby public job board API. Jobs with ``isListed: false`` are skipped.

``publishedAt`` is documented as the *last* publish time, so a re-published job looks fresh.
The spec accepts that; the freshness gate uses it as the posting time.
"""

import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

from jobpulse.core.dedupe import ats_key
from jobpulse.core.locations import normalize_locations
from jobpulse.core.models import CompanyTarget, FetchResult, Posting, Target
from jobpulse.core.text import html_to_text
from jobpulse.sources.base import FetchError, HttpAdapter, parse_timestamp, require_company

API_BASE = "https://api.ashbyhq.com/posting-api/job-board"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_WORKPLACE_TYPES = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}
_COMPENSATION_FIELDS = (
    "compensationTierSummary",
    "scrapeableCompensationSalarySummary",
    "compensationTiers",
    "summaryComponents",
)


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _uuid_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail.lower() if _UUID.fullmatch(tail) else None


def _compensation(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict) and any(value.get(field) for field in _COMPENSATION_FIELDS):
        return value
    return None


class AshbyAdapter(HttpAdapter):
    name = "ashby"

    @staticmethod
    def list_url(token: str) -> str:
        return f"{API_BASE}/{quote(token, safe='')}?includeCompensation=true"

    async def _fetch(self, target: Target, fetched_at: datetime) -> FetchResult:
        company = require_company(target)
        payload = await self.get_json(self.list_url(company.token), use_etag_cache=True)
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            raise FetchError("parse", "expected an object with a 'jobs' list")
        postings: list[Posting] = []
        skipped = 0
        for item in jobs:
            if isinstance(item, dict) and item.get("isListed") is False:
                continue  # unlisted on purpose, not malformed
            posting = self._parse_job(item, company)
            if posting is None:
                skipped += 1
            else:
                postings.append(posting)
        warnings = (
            [f"skipped {skipped} malformed job(s); closures paused this poll"] if skipped else []
        )
        return FetchResult.success(
            postings, fetched_at=fetched_at, complete_listing=skipped == 0, warnings=warnings
        )

    def _parse_job(self, item: Any, company: CompanyTarget) -> Posting | None:
        if not isinstance(item, dict):
            return None
        job_url = _text(item.get("jobUrl"))
        job_id = _text(item.get("id")) or _uuid_from_url(job_url)
        title = _text(item.get("title"))
        if job_id is None or title is None:
            return None
        secondary = [
            entry.get("location") if isinstance(entry, dict) else entry
            for entry in item.get("secondaryLocations") or []
        ]
        locations, _ = normalize_locations(
            [value for value in [item.get("location"), *secondary] if isinstance(value, str)],
            self.aliases,
        )
        # Ashby sets isRemote on Hybrid jobs too, so workplaceType decides.
        workplace = _WORKPLACE_TYPES.get(str(item.get("workplaceType") or "").lower())
        description = _text(item.get("descriptionPlain"))
        if description is None and isinstance(item.get("descriptionHtml"), str):
            description = html_to_text(item["descriptionHtml"])
        posted_at = parse_timestamp(item.get("publishedAt"))
        return Posting(
            source="ashby",
            external_id=job_id,
            company_name=company.name,
            company_token=company.token,
            title=title,
            locations=locations,
            is_remote=None if workplace is None else workplace == "remote",
            workplace_type=workplace,
            url=job_url or f"https://jobs.ashbyhq.com/{company.token}/{job_id}",
            apply_url=_text(item.get("applyUrl")),
            canonical_url=ats_key("ashby", company.token, job_id),
            posted_at=posted_at,
            posted_at_precision="exact" if posted_at else "none",
            description_text=description,
            department=_text(item.get("department")) or _text(item.get("team")),
            compensation=_compensation(item.get("compensation")),
            raw=item,
        )
