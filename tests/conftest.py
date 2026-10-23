import os

import pytest

# Variables jobpulse reads from the environment. Tests start from a clean slate so a
# developer's shell (or a real .env exported into it) can't leak into results.
JOBPULSE_ENV_VARS = (
    "NTFY_TOPIC",
    "NTFY_TOKEN",
    "DISCORD_WEBHOOK_URL",
    "LLM_API_KEY",
    "DASHBOARD_USER",
    "DASHBOARD_PASSWORD",
    "DATABASE_URL",
    "JOBPULSE_CONFIG",
    "LOG_LEVEL",
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("JOBPULSE_LIVE") == "1":
        return
    skip_live = pytest.mark.skip(reason="live network test; set JOBPULSE_LIVE=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in JOBPULSE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
