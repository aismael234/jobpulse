"""Lever Postings API (public). EU-hosted boards (``jobs.eu.lever.co``) answer only on the EU
API host; the US host returns 404 for them. The company row's ``region`` picks the host."""

from datetime import datetime
from typing import Any
from urllib.parse import quote

from jobpulse.core.dedupe import ats_key
from jobpulse.core.locations import normalize_locations
from jobpulse.core.models import CompanyTarget, FetchResult, Posting, Region, Target
from jobpulse.core.text import html_to_text
from jobpulse.core.timeparse import from_epoch_ms
from jobpulse.sources.base import FetchError, HttpAdapter, require_company

API_HOSTS: dict[Region, str] = {
    "us": "https://api.lever.co/v0/postings",
    "eu": "https://api.eu.lever.co/v0/postings",
}
_WORKPLACE_TYPES = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _description(item: dict[str, Any]) -> str | None:
    """The intro, every list (that's where requirements live) and the closing text."""
    parts = [_text(item.get("descriptionPlain"))]
    for entry in item.get("lists") or []:
        if isinstance(entry, dict):
            parts.append(_text(entry.get("text")))
            content = entry.get("content")
            parts.append(html_to_text(content) if isinstance(content, str) else None)
    parts.append(_text(item.get("additionalPlain")))
    text = "\n".join(part for part in parts if part)
    return text or None


class LeverAdapter(HttpAdapter):
    name = "lever"

    @staticmethod
    def list_url(token: str, region: Region = "us") -> str:
        return f"{API_HOSTS[region]}/{quote(token, safe='')}?mode=json"

    async def _fetch(self, target: Target, fetched_at: datetime) -> FetchResult:
        company = require_company(target)
        try:
            payload = await self.get_json(
                self.list_url(company.token, company.region), use_etag_cache=True
            )
        except FetchError as exc:
            if exc.status == 404 and company.region == "us":
                raise FetchError(
                    exc.kind,
                    f"{exc.message} (if the board is on jobs.eu.lever.co, set region: eu)",
                    status=exc.status,
                ) from None
            raise
        if not isinstance(payload, list):
            raise FetchError("parse", "expected a JSON array of postings")
        postings: list[Posting] = []
        skipped = 0
        for item in payload:
            posting = self._parse_posting(item, company)
            if posting is None:
                skipped += 1
            else:
                postings.append(posting)
        warnings = (
            [f"skipped {skipped} malformed posting(s); closures paused this poll"]
            if skipped
            else []
        )
        return FetchResult.success(
            postings, fetched_at=fetched_at, complete_listing=skipped == 0, warnings=warnings
        )

    def _parse_posting(self, item: Any, company: CompanyTarget) -> Posting | None:
        if not isinstance(item, dict):
            return None
        posting_id, title = _text(item.get("id")), _text(item.get("text"))
        if posting_id is None or title is None:
            return None
        raw_categories = item.get("categories")
        categories = raw_categories if isinstance(raw_categories, dict) else {}
        all_locations = categories.get("allLocations")
        raw_locations = (
            [value for value in all_locations if isinstance(value, str)]
            if isinstance(all_locations, list) and all_locations
            else [categories.get("location")]
        )
        locations, located_remote = normalize_locations(
            [value for value in raw_locations if isinstance(value, str)], self.aliases
        )
        workplace = _WORKPLACE_TYPES.get(str(item.get("workplaceType") or "").lower())
        created = item.get("createdAt")
        posted_at = (
            from_epoch_ms(created)
            if isinstance(created, int | float) and not isinstance(created, bool)
            else None
        )
        hosted = (
            _text(item.get("hostedUrl")) or f"https://jobs.lever.co/{company.token}/{posting_id}"
        )
        salary = item.get("salaryRange")
        department = _text(categories.get("department")) or _text(categories.get("team"))
        return Posting(
            source="lever",
            external_id=posting_id,
            company_name=company.name,
            company_token=company.token,
            title=title,
            locations=locations,
            is_remote=(workplace == "remote") if workplace else located_remote,
            workplace_type=workplace,
            url=hosted,
            apply_url=_text(item.get("applyUrl")),
            canonical_url=ats_key("lever", company.token, posting_id),
            posted_at=posted_at,
            posted_at_precision="exact" if posted_at else "none",
            description_text=_description(item),
            department=department,
            compensation=salary if isinstance(salary, dict) and salary else None,
            raw=item,
        )
