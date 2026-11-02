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


def test_compose_runs_one_service_with_everything_in_it() -> None:
    services = compose()["services"]
    assert set(services) == {"jobpulse", "postgres"}
    jobpulse = services["jobpulse"]
    assert jobpulse["command"] == ["jobpulse", "up", "--host", "0.0.0.0"]
    assert jobpulse["restart"] == "unless-stopped"
    # The worker may be inside a LinkedIn request: give it time to finish and release its lease.
    assert jobpulse["stop_grace_period"] == "60s"
    assert "profiles" not in jobpulse


def test_the_dashboard_is_published_on_localhost_only() -> None:
    jobpulse = compose()["services"]["jobpulse"]
    assert jobpulse["ports"] == ["127.0.0.1:8000:8000"]
    assert "healthcheck" in jobpulse


def test_postgres_is_an_optional_profile_that_jobpulse_waits_for() -> None:
    services = compose()["services"]
    assert services["postgres"]["profiles"] == ["postgres"]
    assert "healthcheck" in services["postgres"]
    wait = services["jobpulse"]["depends_on"]["postgres"]
    assert wait == {"condition": "service_healthy", "required": False}


def test_the_data_volume_holds_state_and_config_yaml_is_optional() -> None:
    jobpulse = compose()["services"]["jobpulse"]
    assert jobpulse["volumes"] == ["jobpulse-data:/app/data"]
    assert jobpulse["env_file"] == [{"path": ".env", "required": False}]


def dockerfile() -> str:
    return (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_the_image_runs_as_a_non_root_user_from_the_frozen_lock() -> None:
    text = dockerfile()
    assert "FROM python:3.12-slim" in text
    assert "ghcr.io/astral-sh/uv:0.13.5" in text
    assert "uv sync --frozen --no-dev --extra postgres" in text
    user_lines = [line for line in text.splitlines() if line.startswith("USER ")]
    assert user_lines
    assert user_lines[-1] != "USER root"
    ignored = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for secret in (".env", "config.yaml", "data/"):
        assert secret in ignored


def test_the_image_runs_up_with_the_model_baked_in() -> None:
    text = dockerfile()
    assert 'CMD ["jobpulse", "up", "--host", "0.0.0.0"]' in text
    assert "jobpulse embeddings download --cache-dir /opt/jobpulse/models" in text
    assert "JOBPULSE_MODELS_DIR=/opt/jobpulse/models" in text
    assert "chmod -R a+rX /opt/jobpulse/models" in text  # readable by the non-root user
    # onnxruntime uploads usage events to Microsoft unless told not to (jobpulse sets it too).
    assert "ORT_DISABLE_TELEMETRY=1" in text
    assert "HEALTHCHECK" in text


def test_workflow_actions_are_pinned_to_commit_shas() -> None:
    workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    assert {path.name for path in workflows} == {"ci.yml", "release.yml"}
    for path in workflows:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "uses:" not in line or "uses: ./" in line:
                continue
            action = line.split("uses:", 1)[1].split("#", 1)[0].strip()
            name, _, ref = action.partition("@")
            assert len(ref) == 40, f"{path.name}: {name} isn't pinned to a commit"
            assert int(ref, 16) >= 0
            assert "# v" in line, f"{path.name}: {name} has no tag comment"


def test_the_release_runs_the_checks_and_builds_both_architectures() -> None:
    release = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    )
    jobs = release["jobs"]
    assert jobs["check"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["image"]["needs"] == "check"
    build = jobs["image"]["steps"][-1]["with"]
    assert build["platforms"] == "linux/amd64,linux/arm64"


def test_the_package_version_matches_pyproject() -> None:
    import tomllib

    from jobpulse import __version__

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == __version__
    assert project["project"]["license"] == "MIT"
    assert (REPO_ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
