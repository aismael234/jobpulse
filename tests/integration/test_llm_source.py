"""The LLM layer per profile, resume and key (M6): rebuilt when one changes, absent while
it's off, and waiting for a resume when there's none."""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from jobpulse.config import AppConfig, Settings
from jobpulse.credentials import SecretStore
from jobpulse.db import resume as resume_db
from jobpulse.llm.claude import ClaudeClient
from jobpulse.llm.prompt import build_request
from jobpulse.llm.source import LlmSource
from tests.factories import NOW, make_posting

RESUME = "Backend engineer: Python, Go.\n"


def config(**llm: Any) -> AppConfig:
    return AppConfig.model_validate({"llm": {"enabled": True, **llm}})


def store(key: str | None = "sk-ant-test-0123456789") -> SecretStore:
    """Secrets from these values alone, never from the repository's own .env."""
    return SecretStore(Settings(_env_file=None, llm_api_key=SecretStr(key) if key else None))


@pytest.fixture
def session(database: sessionmaker[Session]) -> Iterator[Session]:
    with database() as session:
        yield session


def put_resume(session: Session, text: str = RESUME) -> None:
    resume_db.put(
        session,
        filename="resume.txt",
        media_type="text/plain",
        text=text,
        lines=["Built Python services"],
        now=NOW - timedelta(days=1),
    )
    session.commit()


async def test_the_layer_is_off_by_default(session: Session) -> None:
    put_resume(session)
    source = LlmSource(store())
    assert await source.current(session, AppConfig()) is None
    assert source.problem is None


async def test_the_layer_reads_the_stored_resume_and_the_profile(session: Session) -> None:
    put_resume(session)
    source = LlmSource(store())
    scorer = await source.current(
        session,
        config(model="claude-sonnet-5-5", effort="medium", band=0.2, wanted_levels=["mid"]),
    )
    assert scorer is not None
    try:
        assert scorer.can_ask
        assert isinstance(scorer._client, ClaudeClient)
        assert (scorer.model, scorer.effort, scorer.band) == ("claude-sonnet-5-5", "medium", 0.2)
        assert scorer.resume == RESUME
        assert scorer.wanted_levels == {"mid"}
        assert scorer.threshold == 0.55
        assert scorer.weight == 0.15
    finally:
        await source.aclose()


async def test_without_a_resume_the_layer_waits_and_says_why(session: Session) -> None:
    source = LlmSource(store())
    assert await source.current(session, config()) is None
    assert source.problem == "no resume stored; upload one on Settings"


async def test_a_dry_run_gets_a_layer_that_only_reads_the_cache(session: Session) -> None:
    put_resume(session)
    source = LlmSource(store(), dry_run=True)
    scorer = await source.current(session, config())
    assert scorer is not None
    assert not scorer.can_ask
    await source.aclose()


async def test_the_scorer_is_kept_until_the_resume_or_the_profile_changes(
    session: Session,
) -> None:
    put_resume(session)
    source = LlmSource(store())
    first = await source.current(session, config())
    assert await source.current(session, config()) is first
    put_resume(session, "Frontend engineer: TypeScript.\n")
    second = await source.current(session, config())
    assert second is not first
    assert second is not None
    assert second.resume.startswith("Frontend")
    third = await source.current(session, config(role="data engineer"))
    assert third is not second
    assert third is not None
    assert third.role == "data engineer"
    await source.aclose()


async def test_a_key_saved_on_settings_is_used(session: Session) -> None:
    put_resume(session)
    secrets = store(None)
    source = LlmSource(secrets)
    without = await source.current(session, config())
    assert without is not None
    assert not without.can_ask
    secrets.save(session, "llm_api_key", "sk-ant-saved-0123456789", now=NOW)
    session.commit()
    with_key = await source.current(session, config())
    assert with_key is not None
    assert with_key.can_ask
    await source.aclose()


def test_the_role_reaches_the_prompt_and_the_cache_key() -> None:
    posting = make_posting()
    backend = build_request(posting, resume=RESUME, model="m", effort="low")
    data = build_request(posting, resume=RESUME, model="m", effort="low", role="data engineer")
    assert "a backend software engineer," in backend.system
    assert "a data engineer," in data.system
    assert backend.input_hash != data.input_hash
