"""Builders shared by the tests."""

import itertools
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from jobspy.model import JobPost

from jobpulse.core.filters import WeightedRegex
from jobpulse.core.models import Posting
from jobpulse.db.tables import PostingRow

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
JOBSPY_COLUMNS = list(JobPost.model_fields)
LINKEDIN_ID = "4101234567"
INDEED_KEY = "1a2b3c4d5e6f7a8b"
_ROW_IDS = itertools.count(5_000_001)


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def compiled(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


def weighted(*pairs: tuple[str, float]) -> tuple[WeightedRegex, ...]:
    return tuple(WeightedRegex(re.compile(p, re.IGNORECASE), w) for p, w in pairs)


def make_posting(**overrides: Any) -> Posting:
    values: dict[str, Any] = {
        "source": "greenhouse",
        "external_id": "1001",
        "company_name": "Acme",
        "company_token": "acme",
        "title": "Backend Engineer",
        "locations": ("San Francisco, CA",),
        "is_remote": False,
        "workplace_type": None,
        "url": "https://job-boards.greenhouse.io/acme/jobs/1001",
        "apply_url": None,
        "canonical_url": "greenhouse:acme:1001",
        "posted_at": NOW - timedelta(minutes=25),
        "posted_at_precision": "exact",
    }
    values.update(overrides)
    if values["posted_at"] is None and "posted_at_precision" not in overrides:
        values["posted_at_precision"] = "none"
    return Posting(**values)


def posting_row(**overrides: Any) -> PostingRow:
    """A stored posting, by default an untriaged digest-tier Acme match seen an hour ago."""
    external_id = str(overrides.pop("external_id", None) or next(_ROW_IDS))
    values: dict[str, Any] = {
        "source": "greenhouse",
        "external_id": external_id,
        "company_name": "Acme",
        "title": "Backend Engineer",
        "locations": ["San Francisco, CA"],
        "is_remote": False,
        "canonical_url": f"greenhouse:acme:{external_id}",
        "fingerprint": f"fingerprint-{external_id}",
        "url": f"https://job-boards.greenhouse.io/acme/jobs/{external_id}",
        "posted_at": NOW - timedelta(hours=2),
        "posted_at_precision": "exact",
        "first_seen_at": NOW - timedelta(hours=1),
        "last_seen_at": NOW,
        "tier": "digest",
        "tier_reason": "score 0.45 < 0.55",
        "score": 0.45,
        "status": "New",
    }
    values.update(overrides)
    return PostingRow(**values)


def jobspy_row(site: str = "linkedin", **overrides: Any) -> dict[str, Any]:
    """One row the way ``scrape_jobs`` 1.4.0 returns it for that site."""
    if site == "linkedin":
        external_id = overrides.pop("external_id", LINKEDIN_ID)
        row: dict[str, Any] = {
            "id": f"li-{external_id}",
            "site": "linkedin",
            "job_url": f"https://www.linkedin.com/jobs/view/{external_id}",
            "job_url_direct": None,
            "title": "Backend Engineer",
            "company": "Acme",
            "location": "San Francisco, CA, USA",
            "date_posted": date(2026, 10, 23),
            "is_remote": False,
            "company_url": "https://www.linkedin.com/company/acme",
        }
    else:
        key = overrides.pop("external_id", INDEED_KEY)
        row = {
            "id": f"in-{key}",
            "site": "indeed",
            "job_url": f"https://www.indeed.com/viewjob?jk={key}",
            "job_url_direct": None,
            "title": "Backend Engineer",
            "company": "Acme",
            "location": "San Francisco, CA, USA",
            "date_posted": date(2026, 10, 23),
            "job_type": "fulltime",
            "salary_source": "direct_data",
            "interval": "yearly",
            "min_amount": 150000.0,
            "max_amount": 190000.0,
            "currency": "USD",
            "is_remote": False,
            "description": "**About the role**\n\nBuild Python services. 3+ years of experience.",
            "company_url": "https://www.indeed.com/cmp/Acme",
            "company_url_direct": "https://acme.example",
        }
    row.update(overrides)
    return row


def jobspy_frame(
    rows: Sequence[Mapping[str, Any]],
    *,
    errors: Mapping[str, str] | None = None,
    warnings: Mapping[str, Sequence[str]] | None = None,
) -> pd.DataFrame:
    """A DataFrame shaped like ``scrape_jobs``' result, ``attrs`` included."""
    frame = pd.DataFrame([dict(row) for row in rows], columns=JOBSPY_COLUMNS)
    frame.attrs = {
        "errors": dict(errors or {}),
        "warnings": {site: list(items) for site, items in (warnings or {}).items()},
    }
    return frame


class FakeSleep:
    """Records requested sleeps instead of sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class FakeMonotonic:
    """A monotonic clock that only moves when told to."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now
