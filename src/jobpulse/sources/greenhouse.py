"""Greenhouse Job Board API (public, no auth).

The list call skips ``content=true``: it already carries ``first_published``, which is what
the freshness gate uses. ``updated_at`` changes on every edit or re-sync and is never used
for age. The per-job call runs only for new postings that survive the title filter.
"""

import dataclasses
import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote

from jobpulse.core.dedupe import ats_key
from jobpulse.core.locations import normalize_locations
from jobpulse.core.models import CompanyTarget, FetchResult, Posting, Target
from jobpulse.core.text import html_to_text
from jobpulse.sources.base import FetchError, HttpAdapter, parse_timestamp, require_company

log = logging.getLogger(__name__)

API_BASE = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseAdapter(HttpAdapter):
    name = "greenhouse"

    @staticmethod
    def list_url(token: str) -> str:
        return f"{API_BASE}/{quote(token, safe='')}/jobs"

    @staticmethod
    def job_url(token: str, job_id: str) -> str:
        return f"{API_BASE}/{quote(token, safe='')}/jobs/{job_id}?pay_transparency=true"

    async def _fetch(self, target: Target, fetched_at: datetime) -> FetchResult:
        company = require_company(target)
        payload = await self.get_json(self.list_url(company.token), use_etag_cache=True)
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            raise FetchError("parse", "expected an object with a 'jobs' list")
        postings: list[Posting] = []
        skipped = 0
        for item in jobs:
            posting = self._parse_job(item, company)
            if posting is None:
                skipped += 1
            else:
                postings.append(posting)
        warnings = (
            [f"skipped {skipped} malformed job(s); closures paused this poll"] if skipped else []
        )
        # A listing with unreadable items isn't complete, so it can't drive closures.
        return FetchResult.success(
            postings, fetched_at=fetched_at, complete_listing=skipped == 0, warnings=warnings
        )

    def _parse_job(self, item: Any, company: CompanyTarget) -> Posting | None:
        if not isinstance(item, dict):
            return None
        job_id, title = item.get("id"), item.get("title")
        if (
            isinstance(job_id, bool)
            or not isinstance(job_id, int | str)
            or not str(job_id).isdigit()
        ):
            return None
        if not isinstance(title, str) or not title.strip():
            return None
        external_id = str(job_id)
        location = item.get("location")
        location_name = location.get("name") if isinstance(location, dict) else None
        locations, is_remote = normalize_locations(
            [location_name] if isinstance(location_name, str) else [], self.aliases
        )
        absolute_url = item.get("absolute_url")
        url = (
            absolute_url
            if isinstance(absolute_url, str) and absolute_url
            else f"https://job-boards.greenhouse.io/{company.token}/jobs/{external_id}"
        )
        posted_at = parse_timestamp(item.get("first_published"))
        return Posting(
            source="greenhouse",
            external_id=external_id,
            company_name=company.name,
            company_token=company.token,
            title=title.strip(),
            locations=locations,
            is_remote=is_remote,
            workplace_type=None,
            url=url,
            apply_url=url,
            canonical_url=ats_key("greenhouse", company.token, external_id),
            posted_at=posted_at,
            posted_at_precision="exact" if posted_at else "none",
            raw=item,
        )

    async def enrich(self, posting: Posting) -> Posting:
        """Add the description, department and pay ranges from the per-job endpoint."""
        if posting.source != "greenhouse" or not posting.company_token:
            return posting
        try:
            payload = await self.get_json(self.job_url(posting.company_token, posting.external_id))
        except FetchError as exc:
            log.warning(
                "greenhouse detail %s/%s failed: %s",
                posting.company_token, posting.external_id, exc.message,
            )  # fmt: skip
            return posting
        if not isinstance(payload, dict):
            return posting
        content = payload.get("content")
        description = html_to_text(content) if isinstance(content, str) else None
        departments = [
            department["name"]
            for department in payload.get("departments") or []
            if isinstance(department, dict) and isinstance(department.get("name"), str)
        ]
        pay_ranges = payload.get("pay_input_ranges")
        posted_at = posting.posted_at or parse_timestamp(payload.get("first_published"))
        return dataclasses.replace(
            posting,
            description_text=description or posting.description_text,
            department=departments[0] if departments else posting.department,
            compensation=(
                {"pay_input_ranges": pay_ranges}
                if isinstance(pay_ranges, list) and pay_ranges
                else posting.compensation
            ),
            posted_at=posted_at,
            posted_at_precision="exact" if posted_at else "none",
        )
