"""The LLM layer for the current profile, resume and API key (M6). The scorer is rebuilt when
one of them changes, so turning the layer on, uploading a resume or adding a key on Settings
takes effect at the next cycle, with no restart."""

import hashlib
from collections.abc import Callable

from sqlalchemy.orm import Session

from jobpulse.config import AppConfig, LlmEffort
from jobpulse.credentials import SecretStore
from jobpulse.db import resume as resume_db
from jobpulse.llm.base import LlmClient
from jobpulse.llm.claude import ClaudeClient
from jobpulse.llm.scorer import LlmScorer

ClientFactory = Callable[[str, str, LlmEffort], LlmClient]  # (api key, model, effort)


def _claude(key: str, model: str, effort: LlmEffort) -> LlmClient:
    return ClaudeClient(api_key=key, model=model, effort=effort)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class LlmSource:
    """Hands the poller the scorer for this cycle, or ``None`` while the layer is off or has
    no resume (``problem`` says which). Dry runs get no client: they only read cached
    answers."""

    def __init__(
        self, store: SecretStore, *, dry_run: bool = False, client: ClientFactory = _claude
    ) -> None:
        self._store = store
        self._dry_run = dry_run
        self._client = client
        self._identity: tuple[str, ...] | None = None
        self._scorer: LlmScorer | None = None
        self.problem: str | None = None

    async def current(self, session: Session, config: AppConfig) -> LlmScorer | None:
        llm = config.llm
        self.problem = None
        resume = resume_db.text(session) if llm.enabled else None
        if not llm.enabled or not resume:
            if llm.enabled:
                self.problem = "no resume stored; upload one on Settings"
            await self._replace(None, None)
            return None
        key = self._store.resolve(session).get("llm_api_key") or ""
        weights = config.scoring
        identity = (
            llm.model_dump_json(),
            f"{weights.instant_threshold}:{weights.weights.llm}",
            _digest(resume),
            _digest(key),
        )
        if identity == self._identity:
            return self._scorer
        client = self._client(key, llm.model, llm.effort) if key and not self._dry_run else None
        scorer = LlmScorer(
            client,
            resume=resume,
            model=llm.model,
            effort=llm.effort,
            band=llm.band,
            threshold=weights.instant_threshold,
            weight=weights.weights.llm,
            wanted_levels=llm.wanted_levels,
            max_calls_per_day=llm.max_calls_per_day,
            role=llm.role,
        )
        await self._replace(identity, scorer)
        return scorer

    async def _replace(self, identity: tuple[str, ...] | None, scorer: LlmScorer | None) -> None:
        old, self._scorer, self._identity = self._scorer, scorer, identity
        if old is not None and old is not scorer:
            await old.aclose()

    async def aclose(self) -> None:
        await self._replace(None, None)
