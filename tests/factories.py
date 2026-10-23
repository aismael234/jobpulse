"""Builders shared by the tests."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jobpulse.core.filters import WeightedRegex
from jobpulse.core.models import Posting

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)


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


class FakeSleep:
    """Records requested sleeps instead of sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
