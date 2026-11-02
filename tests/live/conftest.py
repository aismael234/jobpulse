"""Shared by the live tests: an HttpClient whose User-Agent carries $JOBPULSE_CONTACT_EMAIL, or
the contact_email an old ./config.yaml still holds (a throwaway example.com address
otherwise). The live database is never read."""

import os
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml

from jobpulse.httpclient import HttpClient, build_user_agent

CONFIG = Path(__file__).resolve().parents[2] / "config.yaml"


def contact_email() -> str:
    if email := os.environ.get("JOBPULSE_CONTACT_EMAIL"):
        return email
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else None
    if isinstance(data, dict) and isinstance(data.get("contact_email"), str):
        return str(data["contact_email"])
    return f"jobpulse-dev-{secrets.token_hex(4)}@example.com"


@pytest.fixture
async def live_http() -> AsyncIterator[HttpClient]:
    async with HttpClient(user_agent=build_user_agent(contact_email())) as client:
        yield client
