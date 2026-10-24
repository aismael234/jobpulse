"""M4: the LLM layer picks the band decisions, asks a few at a time, and stops for good on a
fatal error (a bad key)."""

import asyncio

import pytest

from jobpulse.core.assessment import Assessment
from jobpulse.core.models import Decision
from jobpulse.llm.base import AssessmentRequest, LlmReply
from jobpulse.llm.prompt import build_request
from jobpulse.llm.scorer import LlmScorer
from tests.factories import make_posting

RESUME = "Backend engineer: Python, Go, Postgres."


class FakeClient:
    model = "claude-opus-5-5"

    def __init__(self, *replies: LlmReply) -> None:
        self.replies = list(replies)
        self.requests: list[AssessmentRequest] = []
        self.running = 0
        self.most = 0
        self.closed = False

    async def assess(self, request: AssessmentRequest) -> LlmReply:
        self.requests.append(request)
        self.running += 1
        self.most = max(self.most, self.running)
        await asyncio.sleep(0)
        self.running -= 1
        if self.replies:
            return self.replies.pop(0)
        return LlmReply(model=self.model, assessment=Assessment("mid", 0.8, 0.8))

    async def aclose(self) -> None:
        self.closed = True


def scorer(client: FakeClient | None, **options: object) -> LlmScorer:
    values: dict[str, object] = {
        "resume": RESUME,
        "model": "claude-opus-5-5",
        "effort": "low",
        "band": 0.10,
        "threshold": 0.55,
        "weight": 0.15,
        **options,
    }
    return LlmScorer(client, **values)  # type: ignore[arg-type]


def decision(score: float | None, tier: str = "") -> Decision:
    if score is None:
        return Decision(tier=tier or "filtered", reason="title matches no include rule")  # type: ignore[arg-type]
    return Decision(
        tier=tier or ("instant" if score >= 0.55 else "digest"),  # type: ignore[arg-type]
        reason=f"score {score:.2f}",
        score=score,
        breakdown={"total": score},
    )


def test_candidates_are_the_band_closest_to_the_threshold_first() -> None:
    postings = [make_posting(external_id=str(n)) for n in range(6)]
    decisions = [
        decision(0.30),
        decision(0.60),
        decision(None),
        decision(0.54),
        decision(0.80),
        decision(0.47),
    ]
    asks = scorer(FakeClient()).candidates(postings, decisions)
    assert [ask.index for ask in asks] == [3, 1, 5]
    assert asks[0].request == build_request(
        postings[3], resume=RESUME, model="claude-opus-5-5", effort="low"
    )


async def test_ask_answers_in_order_a_few_at_a_time() -> None:
    client = FakeClient()
    llm = scorer(client, concurrency=2)
    requests = [
        build_request(make_posting(external_id=str(n)), resume=RESUME, model="m", effort="low")
        for n in range(5)
    ]
    replies = await llm.ask(requests)
    assert len(replies) == 5
    assert client.requests == requests
    assert client.most == 2


async def test_a_fatal_reply_stops_asking_until_a_restart() -> None:
    client = FakeClient(LlmReply(model="m", error="HTTP 401: invalid x-api-key", fatal=True))
    llm = scorer(client, concurrency=1)
    requests = [
        build_request(make_posting(external_id=str(n)), resume=RESUME, model="m", effort="low")
        for n in range(3)
    ]
    replies = await llm.ask(requests)
    assert len(client.requests) == 1
    assert [reply.assessment for reply in replies] == [None, None, None]
    assert [reply.asked for reply in replies] == [True, False, False]  # only one call to count
    assert llm.stopped is not None
    assert "401" in llm.stopped
    assert not llm.can_ask
    [skipped] = await llm.ask(requests[:1])
    assert not skipped.asked
    assert len(client.requests) == 1


def test_without_a_client_nothing_can_be_asked() -> None:
    assert not scorer(None).can_ask
    assert scorer(FakeClient()).can_ask


def test_apply_moves_the_score() -> None:
    llm = scorer(FakeClient(), wanted_levels=("entry", "mid"))
    lifted = llm.apply(decision(0.50), Assessment("mid", 1.0, 1.0))
    assert lifted.tier == "instant"
    assert lifted.score == pytest.approx(0.65)
    dropped = llm.apply(decision(0.60), Assessment("senior", 1.0, 1.0))
    assert dropped.tier == "digest"
    assert dropped.score == pytest.approx(0.45)


async def test_aclose_closes_the_client() -> None:
    client = FakeClient()
    await scorer(client).aclose()
    assert client.closed
