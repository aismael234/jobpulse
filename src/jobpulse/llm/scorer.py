"""The LLM scoring layer: a second opinion on the matches whose score sits within ``band`` of
``scoring.instant_threshold``. The poller owns the database side (the per-posting cache
and the daily call count); this decides what to ask, asks with a little concurrency, and
applies the answers."""

import asyncio
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from jobpulse.core.assessment import Assessment, apply_assessment, needs_assessment
from jobpulse.core.models import Decision, Posting
from jobpulse.llm.base import AssessmentRequest, LlmClient, LlmReply
from jobpulse.llm.prompt import build_request

DEFAULT_CONCURRENCY = 3


@dataclass(frozen=True, slots=True)
class Ask:
    index: int  # into the cycle's survivors
    request: AssessmentRequest


class LlmScorer:
    def __init__(
        self,
        client: LlmClient | None,
        *,
        resume: str,
        model: str,
        effort: str,
        band: float,
        threshold: float,
        weight: float,
        wanted_levels: Collection[str] = (),
        max_calls_per_day: int = 25,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        """``client`` may be ``None`` for dry runs, which only read the cache."""
        self._client = client
        self.resume = resume
        self.model = model
        self.effort = effort
        self.band = band
        self.threshold = threshold
        self.weight = weight
        self.wanted_levels = frozenset(wanted_levels)
        self.max_calls_per_day = max_calls_per_day
        self._semaphore = asyncio.Semaphore(concurrency)
        self.stopped: str | None = None  # why asking stopped for this process (a bad key)

    @property
    def can_ask(self) -> bool:
        return self._client is not None and self.stopped is None

    def candidates(self, postings: Sequence[Posting], decisions: Sequence[Decision]) -> list[Ask]:
        """The decisions within the band, closest to the threshold first."""
        indexes = [
            index
            for index, decision in enumerate(decisions)
            if needs_assessment(decision, threshold=self.threshold, band=self.band)
        ]
        indexes.sort(key=lambda index: abs((decisions[index].score or 0.0) - self.threshold))
        return [
            Ask(
                index,
                build_request(
                    postings[index], resume=self.resume, model=self.model, effort=self.effort
                ),
            )
            for index in indexes
        ]

    async def ask(self, requests: Sequence[AssessmentRequest]) -> list[LlmReply]:
        """One reply per request, in order; a fatal reply stops further asking."""
        return list(await asyncio.gather(*(self._ask_one(request) for request in requests)))

    async def _ask_one(self, request: AssessmentRequest) -> LlmReply:
        async with self._semaphore:
            if self._client is None or self.stopped is not None:
                reason = self.stopped or "no client"
                return LlmReply(model=self.model, error=f"not asked: {reason}", asked=False)
            reply = await self._client.assess(request)
            if reply.fatal and self.stopped is None:
                self.stopped = reply.error or "fatal error"
        return reply

    def apply(self, decision: Decision, assessment: Assessment) -> Decision:
        return apply_assessment(
            decision,
            assessment,
            weight=self.weight,
            wanted_levels=self.wanted_levels,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
