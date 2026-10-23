"""Shared by the live tests: an HttpClient whose User-Agent carries the contact_email from
./config.yaml when it exists (a throwaway example.com address otherwise)."""

import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jobpulse.config import load_config
from jobpulse.httpclient import HttpClient, build_user_agent

CONFIG = Path(__file__).resolve().parents[2] / "config.yaml"


def contact_email() -> str:
    if CONFIG.exists():
        return load_config(CONFIG).contact_email
    return f"jobpulse-dev-{secrets.token_hex(4)}@example.com"


@pytest.fixture
async def live_http() -> AsyncIterator[HttpClient]:
    async with HttpClient(user_agent=build_user_agent(contact_email())) as client:
        yield client
