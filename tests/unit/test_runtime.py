"""M4 wiring: the Workday adapter, LinkedIn job pages behind the shared gate, and the LLM
layer, which exists only when it's switched on."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from jobpulse.config import AppConfig, Settings, scoring_weights
from jobpulse.embeddings.base import EmbedderUnavailable
from jobpulse.httpclient import HttpClient
from jobpulse.llm.claude import ClaudeClient
from jobpulse.runtime import (
    KNOWN_SOURCES,
    WATCHLIST_SOURCES,
    build_adapters,
    build_embedder,
    build_llm,
)
from jobpulse.sources.jobspy_boards import JobSpyAdapter
from jobpulse.sources.linkedin_guest import LinkedInSource
from jobpulse.sources.workday import WorkdayAdapter


def config(tmp_path: Path, **sections: object) -> AppConfig:
    return AppConfig.model_validate({"contact_email": "dev@example.com", **sections})


def settings(key: str | None = "sk-ant-test-0123456789") -> Settings:
    """Settings from these values alone, never from the repository's own .env."""
    return Settings(_env_file=None, llm_api_key=SecretStr(key) if key else None)


def llm_on(tmp_path: Path, **llm: object) -> AppConfig:
    resume = tmp_path / "resume.txt"
    resume.write_text("Backend engineer: Python, Go.\n", encoding="utf-8")
    return config(tmp_path, llm={"enabled": True, "resume_path": str(resume), **llm})


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


def test_the_llm_layer_is_off_by_default(tmp_path: Path) -> None:
    assert build_llm(config(tmp_path), settings()) is None


async def test_the_llm_layer_when_it_is_on(tmp_path: Path) -> None:
    scorer = build_llm(
        llm_on(
            tmp_path, model="claude-sonnet-5-5", effort="medium", band=0.2, wanted_levels=["mid"]
        ),
        settings(),
    )
    assert scorer is not None
    try:
        assert scorer.can_ask
        assert isinstance(scorer._client, ClaudeClient)
        assert (scorer.model, scorer.effort, scorer.band) == ("claude-sonnet-5-5", "medium", 0.2)
        assert scorer.resume == "Backend engineer: Python, Go.\n"
        assert scorer.wanted_levels == {"mid"}
        assert scorer.threshold == 0.55
        assert scorer.weight == 0.15
    finally:
        await scorer.aclose()


async def test_a_dry_run_gets_a_layer_that_only_reads_the_cache(tmp_path: Path) -> None:
    scorer = build_llm(llm_on(tmp_path), settings(None), dry_run=True)
    assert scorer is not None
    assert not scorer.can_ask
    await scorer.aclose()


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
