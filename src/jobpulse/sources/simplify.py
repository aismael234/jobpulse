"""The SimplifyJobs New-Grad list: one JSON array of every listing, open or closed.

The feed is about 13 MB (2 MB gzipped) and changes about every 30 minutes, so each fetch
sends the stored ETag in ``If-None-Match``; a 304 is a successful "unchanged" run. Only
``active && is_visible`` listings count as open, and a listing that shows ``active: false``
is reported closed. ``date_posted`` (Unix seconds) is when the row joined the list; values
that fall exactly on midnight UTC are day precision.
"""

from datetime import datetime
from typing import Any

from jobpulse.core.dedupe import canonicalize_url
from jobpulse.core.locations import normalize_locations
from jobpulse.core.models import FetchResult, Posting, Precision, Target
from jobpulse.core.timeparse import from_epoch_seconds
from jobpulse.sources.base import FetchError, HttpAdapter, require_feed

# The list's own README script folds these long-form names into the short ones.
CATEGORY_ALIASES = {
    "software engineering": "Software",
    "data science, ai & machine learning": "AI/ML/Data",
}
_SECONDS_PER_DAY = 86_400


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def normalize_category(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    return CATEGORY_ALIASES.get(text.casefold(), text)


def _posted(value: Any) -> tuple[datetime | None, Precision]:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None, "none"
    # Rows added by date only sit exactly on midnight UTC.
    return from_epoch_seconds(value), "day" if value % _SECONDS_PER_DAY == 0 else "exact"


class SimplifyAdapter(HttpAdapter):
    name = "simplify"

    async def _fetch(self, target: Target, fetched_at: datetime) -> FetchResult:
        feed = require_feed(target)
        headers = {"If-None-Match": feed.etag} if feed.etag else None
        response = await self.get_response(feed.url, headers=headers, accept=(200, 304))
        if response.status_code == 304:
            if not feed.etag:
                raise FetchError("other", "HTTP 304 although no ETag was sent", status=304)
            return FetchResult.unchanged(fetched_at=fetched_at, etag=feed.etag)
        payload = self.decode_json(response)
        if not isinstance(payload, list):
            raise FetchError("parse", "expected a JSON array of listings")

        postings: list[Posting] = []
        closed: list[str] = []
        skipped = 0
        for item in payload:
            active = item.get("active") if isinstance(item, dict) else None
            if active is False:
                if listing_id := _text(item.get("id")):
                    closed.append(listing_id)
                continue
            if active is True and item.get("is_visible") is False:
                continue  # hidden by the list's maintainers: neither open nor closed
            posting = self.parse_listing(item) if active is True else None
            if posting is None:
                skipped += 1
            else:
                postings.append(posting)
        warnings = [f"skipped {skipped} malformed listing(s)"] if skipped else []
        return FetchResult.success(
            postings,
            fetched_at=fetched_at,
            complete_listing=False,  # closures come from active: false, never from absence
            closed_external_ids=closed,
            etag=response.headers.get("ETag"),
            warnings=warnings,
        )

    def parse_listing(self, item: Any) -> Posting | None:
        if not isinstance(item, dict):
            return None
        listing_id, title = _text(item.get("id")), _text(item.get("title"))
        company, url = _text(item.get("company_name")), _text(item.get("url"))
        if listing_id is None or title is None or company is None or url is None:
            return None
        raw_locations = item.get("locations")
        locations, is_remote = normalize_locations(
            [value for value in raw_locations if isinstance(value, str)]
            if isinstance(raw_locations, list)
            else [],
            self.aliases,
        )
        posted_at, precision = _posted(item.get("date_posted"))
        return Posting(
            source="simplify",
            external_id=listing_id,
            company_name=company,
            company_token=None,
            title=title,
            locations=locations,
            is_remote=is_remote,
            workplace_type=None,
            url=url,
            apply_url=url,
            canonical_url=canonicalize_url(url),
            posted_at=posted_at,
            posted_at_precision=precision,
            department=normalize_category(item.get("category")),
            raw=item,
        )
