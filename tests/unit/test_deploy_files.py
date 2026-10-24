"""Static checks on what ships next to the code: Docker files and the vendored htmx."""

import hashlib
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
HTMX = REPO_ROOT / "src" / "jobpulse" / "web" / "static" / "htmx.min.js"
# htmx.org 2.0.11 from the npm registry (tarball sha512 checked against the registry on
# 2026-10-24); this is the sha256 of dist/htmx.min.js inside it.
HTMX_SHA256 = "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"


def compose() -> dict[str, Any]:
    loaded = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_vendored_htmx_is_the_checked_release() -> None:
    assert hashlib.sha256(HTMX.read_bytes()).hexdigest() == HTMX_SHA256
    assert 'version:"2.0.11"' in HTMX.read_text(encoding="utf-8")


def test_compose_runs_migrations_before_the_worker_and_the_dashboard() -> None:
    services = compose()["services"]
    assert services["migrate"]["command"] == ["jobpulse", "db", "upgrade"]
    for name in ("worker", "web"):
        assert services[name]["depends_on"]["migrate"]["condition"] == (
            "service_completed_successfully"
        )
        assert services[name]["restart"] == "unless-stopped"
    assert services["worker"]["command"] == ["jobpulse", "worker"]


def test_the_dashboard_is_published_on_localhost_only() -> None:
    web = compose()["services"]["web"]
    assert web["ports"] == ["127.0.0.1:8000:8000"]
    assert web["command"][:2] == ["jobpulse", "web"]
    assert "healthcheck" in web


def test_postgres_is_an_optional_profile_that_migrations_wait_for() -> None:
    services = compose()["services"]
    assert services["postgres"]["profiles"] == ["postgres"]
    assert "healthcheck" in services["postgres"]
    wait = services["migrate"]["depends_on"]["postgres"]
    assert wait == {"condition": "service_healthy", "required": False}
    for name in ("migrate", "worker", "web"):
        assert "profiles" not in services[name]


def test_services_share_one_data_volume_and_read_secrets_from_env() -> None:
    services = compose()["services"]
    config_mount = {
        "type": "bind",
        "source": "./config.yaml",
        "target": "/app/config.yaml",
        "read_only": True,
        # A missing config.yaml is an error, not a directory Docker creates in its place.
        "bind": {"create_host_path": False},
    }
    for name in ("migrate", "worker", "web"):
        service = services[name]
        assert "jobpulse-data:/app/data" in service["volumes"]
        assert config_mount in service["volumes"]
        assert service["env_file"] == [{"path": ".env", "required": False}]


def test_the_image_runs_as_a_non_root_user_from_the_frozen_lock() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in dockerfile
    assert "ghcr.io/astral-sh/uv:0.13.5" in dockerfile
    assert "uv sync --frozen --no-dev --extra postgres" in dockerfile
    user_lines = [line for line in dockerfile.splitlines() if line.startswith("USER ")]
    assert user_lines
    assert user_lines[-1] != "USER root"
    ignored = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for secret in (".env", "config.yaml", "data/"):
        assert secret in ignored
