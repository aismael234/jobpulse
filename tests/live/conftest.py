"""Shared by the live tests: an HttpClient whose User-Agent carries $JOBPULSE_CONTACT_EMAIL (a
throwaway example.com address otherwise). The live database is never read."""

import os
import secrets
from collections.abc import AsyncIterator

import pytest

from jobpulse.httpclient import HttpClient, build_user_agent


def contact_email() -> str:
    if email := os.environ.get("JOBPULSE_CONTACT_EMAIL"):
        return email
    return f"jobpulse-dev-{secrets.token_hex(4)}@example.com"


@pytest.fixture
async def live_http() -> AsyncIterator[HttpClient]:
    async with HttpClient(user_agent=build_user_agent(contact_email())) as client:
        yield client
