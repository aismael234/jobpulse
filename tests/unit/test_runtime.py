"""Wiring: the Workday adapter, LinkedIn job pages behind the shared gate, the embedder, and
what a new profile revision changes outside the poller (M6). The LLM source is tested in
tests/integration/test_llm_source.py."""

from pathlib import Path

import pytest

from jobpulse.config import AppConfig, scoring_weights
from jobpulse.core.locations import merge_aliases
from jobpulse.embeddings.base import EmbedderUnavailable
from jobpulse.httpclient import HttpClient
from jobpulse.runtime import (
    KNOWN_SOURCES,
    WATCHLIST_SOURCES,
    build_adapters,
    build_embedder,
    follow_profile,
    scheduled_jobs,
)
from jobpulse.sources.jobspy_boards import JobSpyAdapter
from jobpulse.sources.linkedin_guest import LinkedInSource
from jobpulse.sources.workday import WorkdayAdapter


def config(tmp_path: Path, **sections: object) -> AppConfig:
    return AppConfig.model_validate({"contact_email": "dev@example.com", **sections})


def test_workday_is_a_watchlist_source() -> None:
    assert "workday" in WATCHLIST_SOURCES
    assert "workday" in KNOWN_SOURCES


def test_adapters_include_workday_and_linkedin_job_pages(tmp_path: Path, http: HttpClient) -> None:
    adapters = build_adapters(http, config(tmp_path, workday={"max_pages": 7}))
    workday = adapters["workday"]
    assert isinstance(workday, WorkdayAdapter)
    assert workday.max_pages == 7
    assert isinstance(adapters["linkedin"], LinkedInSource)
    assert isinstance(adapters["indeed"], JobSpyAdapter)


def test_linkedin_job_pages_can_be_turned_off(tmp_path: Path, http: HttpClient) -> None:
    adapters = build_adapters(http, config(tmp_path, linkedin={"job_pages": False}))
    assert isinstance(adapters["linkedin"], JobSpyAdapter)
    assert adapters["linkedin"] is adapters["indeed"]


def test_a_new_profile_updates_the_user_agent_and_the_shared_aliases(
    tmp_path: Path, http: HttpClient
) -> None:
    aliases = merge_aliases({})
    adapters = build_adapters(http, config(tmp_path), aliases=aliases)
    apply = follow_profile(http, aliases)
    apply(
        config(
            tmp_path,
            contact_email="new@example.com",
            filters={"locations": {"aliases": {"Bay": "San Francisco, CA"}}},
        )
    )
    assert "contact: new@example.com" in http.user_agent
    assert aliases["bay"] == "San Francisco, CA"
    assert getattr(adapters["greenhouse"], "aliases") is aliases  # noqa: B009 - all share it


def test_the_job_board_job_is_scheduled_even_without_searches(tmp_path: Path) -> None:
    """Searches live in the profile and can appear while the worker runs."""

    async def run(job: str, sources: tuple[str, ...]) -> None:
        return None

    assert set(scheduled_jobs(config(tmp_path), run)) == {"watchlist", "simplify", "job_boards"}
    off = config(tmp_path, simplify={"enabled": False})
    assert set(scheduled_jobs(off, run)) == {"watchlist", "job_boards"}


# --- M5b: the embedder --------------------------------------------------------------


def test_the_embedder_is_built_lazily_and_never_loads_until_used(tmp_path: Path) -> None:
    """Building it costs nothing: an install without a resume never loads the model."""
    embedder = build_embedder(config(tmp_path, embeddings={"cache_dir": str(tmp_path / "m")}))
    assert embedder is not None
    assert embedder.model.startswith("BAAI/bge-small-en-v1.5@fastembed-")
    with pytest.raises(EmbedderUnavailable, match="jobpulse embeddings download"):
        embedder.embed(["Python services"])  # nothing in the cache dir


def test_embeddings_can_be_turned_off(tmp_path: Path) -> None:
    assert build_embedder(config(tmp_path, embeddings={"enabled": False})) is None


def test_the_resume_weight_reaches_the_scoring_weights(tmp_path: Path) -> None:
    assert scoring_weights(config(tmp_path)).resume == 0.25
    tuned = config(tmp_path, scoring={"weights": {"resume": 0.1}})
    assert scoring_weights(tuned).resume == 0.1
