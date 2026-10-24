"""M4: the Claude client, over the real Anthropic SDK with a mock transport (no network). The
request carries the structured-output schema, the effort and the refusal fallback; the
answer, a refusal, a cut-off answer and API errors all come back as replies."""

import json
from typing import Any

import anthropic
import httpx2
import pytest

from jobpulse.llm.base import AssessmentRequest
from jobpulse.llm.claude import FALLBACK_BETA, MAX_TOKENS, ClaudeClient

KEY = "sk-ant-test-0123456789"
REQUEST = AssessmentRequest(
    system="You help one job seeker.",
    resume="RESUME: Python, Go, Postgres, 4 years.",
    posting="<posting>\nCompany: Acme\nTitle: Backend Engineer\n</posting>",
    input_hash="0" * 64,
)
ANSWER = {"level": "mid", "backend_focus": 90, "fit": 75, "reason": "Python APIs match."}


def message(
    answer: Any = ANSWER,
    *,
    stop_reason: str = "end_turn",
    model: str = "claude-opus-5-5",
    text: str | None = None,
) -> httpx2.Response:
    content: list[dict[str, Any]] = [{"type": "thinking", "thinking": "", "signature": "c2ln"}]
    if answer is not None or text is not None:
        content.append({"type": "text", "text": text if text is not None else json.dumps(answer)})
    return httpx2.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 180,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 900,
            },
        },
    )


class FakeApi:
    def __init__(self, *responses: httpx2.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def client(api: FakeApi, *, model: str = "claude-opus-5-5", effort: str = "low") -> ClaudeClient:
    return ClaudeClient(
        api_key=KEY,
        model=model,
        effort=effort,  # type: ignore[arg-type]
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(api)),
    )


async def test_the_request() -> None:
    api = FakeApi(message())
    await client(api).assess(REQUEST)

    [request] = api.requests
    assert request.method == "POST"
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == KEY
    assert FALLBACK_BETA in request.headers["anthropic-beta"]
    body = api.body()
    assert body["model"] == "claude-opus-5-5"
    assert body["max_tokens"] == MAX_TOKENS
    assert body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "low"
    output_format = body["output_config"]["format"]
    assert output_format["type"] == "json_schema"
    schema = output_format["schema"]
    assert set(schema["required"]) == {"level", "backend_focus", "fit", "reason"}
    assert schema["additionalProperties"] is False
    assert "unknown" in schema["properties"]["level"]["enum"]
    instructions, resume = body["system"]
    assert instructions == {"type": "text", "text": REQUEST.system}
    assert REQUEST.resume in resume["text"]
    assert resume["cache_control"] == {"type": "ephemeral"}  # the shared prefix is cached
    assert body["messages"] == [{"role": "user", "content": REQUEST.posting}]
    for absent in ("thinking", "temperature", "top_p", "top_k", "tools"):
        assert absent not in body


async def test_the_answer_becomes_an_assessment() -> None:
    reply = await client(FakeApi(message())).assess(REQUEST)
    assert reply.error is None
    assert reply.assessment is not None
    assert reply.assessment.level == "mid"
    assert reply.assessment.backend_focus == pytest.approx(0.9)
    assert reply.assessment.fit == pytest.approx(0.75)
    assert reply.assessment.reason == "Python APIs match."
    assert reply.model == "claude-opus-5-5"
    assert reply.input_tokens == 1200 + 900  # cached input counts too
    assert reply.output_tokens == 180
    assert not reply.fatal


async def test_scores_outside_0_to_100_are_clamped() -> None:
    answer = {**ANSWER, "backend_focus": 140, "fit": -5}
    reply = await client(FakeApi(message(answer))).assess(REQUEST)
    assert reply.assessment is not None
    assert (reply.assessment.backend_focus, reply.assessment.fit) == (1.0, 0.0)


async def test_the_effort_comes_from_the_client() -> None:
    api = FakeApi(message())
    await client(api, effort="high").assess(REQUEST)
    assert api.body()["output_config"]["effort"] == "high"


async def test_a_model_without_server_side_fallback_gets_none() -> None:
    api = FakeApi(message(model="claude-haiku-5-5"))
    reply = await client(api, model="claude-haiku-5-5").assess(REQUEST)
    assert reply.assessment is not None
    assert "fallbacks" not in api.body()
    assert FALLBACK_BETA not in api.requests[-1].headers.get("anthropic-beta", "")


async def test_an_answer_from_the_fallback_model_says_so() -> None:
    reply = await client(FakeApi(message(model="claude-opus-4-8"))).assess(REQUEST)
    assert reply.assessment is not None
    assert reply.model == "claude-opus-4-8"


async def test_a_refusal_is_an_error() -> None:
    reply = await client(FakeApi(message(None, stop_reason="refusal"))).assess(REQUEST)
    assert reply.assessment is None
    assert reply.error is not None
    assert "refus" in reply.error
    assert not reply.fatal


async def test_a_cut_off_answer_is_an_error() -> None:
    cut = message(text='{"level": "mid", "backend_fo', stop_reason="max_tokens")
    reply = await client(FakeApi(cut)).assess(REQUEST)
    assert reply.assessment is None
    assert reply.error is not None
    assert "max_tokens" in reply.error


async def test_an_unreadable_answer_is_an_error() -> None:
    reply = await client(FakeApi(message(text="I think it's a good fit!"))).assess(REQUEST)
    assert reply.assessment is None
    assert reply.error is not None


@pytest.mark.parametrize(
    ("status", "fatal"),
    [(400, True), (401, True), (403, True), (404, True), (429, False), (500, False), (529, False)],
)
async def test_api_errors_are_replies(status: int, fatal: bool) -> None:
    error = {"type": "error", "error": {"type": "some_error", "message": f"status {status}"}}
    reply = await client(FakeApi(httpx2.Response(status, json=error))).assess(REQUEST)
    assert reply.assessment is None
    assert reply.error is not None
    assert str(status) in reply.error
    assert reply.fatal is fatal
    assert KEY not in reply.error


async def test_a_network_error_is_a_reply() -> None:
    reply = await client(FakeApi(httpx2.ConnectError("down"))).assess(REQUEST)
    assert reply.assessment is None
    assert reply.error is not None
    assert not reply.fatal
