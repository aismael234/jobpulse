"""The LLM layer's Claude client, on Anthropic's Python SDK.

One structured-output request per posting: the answer has to match ``Verdict``'s JSON schema.
The client checks ``stop_reason`` before it reads the answer, so a refusal or an answer cut off
at ``max_tokens`` is reported as one. Thinking stays on (current models don't turn it off);
``effort`` decides how much. The instructions and the resume are a cached prefix, so a burst of
postings pays for the resume once. Models that support it get the server-side refusal fallback
(``fallbacks: "default"``): a declined request is answered by the fallback model in the same
call. The SDK retries 429s, 5xx and network errors itself; anything still failing becomes an
error reply, and errors that say the key, the account or the request is wrong are fatal.
"""

import dataclasses
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError

from jobpulse.core.assessment import Assessment, Level
from jobpulse.llm.base import AssessmentRequest, LlmReply
from jobpulse.llm.prompt import resume_block

DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOKENS = 4096  # thinking counts toward it; the answer itself is a few dozen tokens
Effort = Literal["low", "medium", "high", "xhigh", "max"]
# Models with server-side refusal fallbacks in "default" mode (Claude Haiku has none).
FALLBACK_MODELS = frozenset(
    {"claude-fable-5-1", "claude-fable-5", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
)
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Every posting would fail the same way: the key, the account, the model or the request.
FATAL_STATUSES = frozenset({400, 401, 403, 404})


class Verdict(BaseModel):
    """The structured output Claude fills in."""

    level: Level = Field(description="The seniority the role actually hires for.")
    backend_focus: int = Field(description="0 to 100: how much of the work is backend.")
    fit: int = Field(description="0 to 100: how well the resume matches the posting.")
    reason: str = Field(description="One short sentence on what decided the fit.")


VERDICT_SCHEMA: dict[str, Any] = {**Verdict.model_json_schema(), "additionalProperties": False}


def _share(value: int) -> float:
    return max(0.0, min(1.0, value / 100))


def _detail(exc: anthropic.APIStatusError) -> str:
    """The API's own error message, never the request (which holds nothing secret anyway)."""
    body = exc.body
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return str(message) if message else type(exc).__name__


class ClaudeClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        effort: Effort = "low",
        max_retries: int = 2,
        timeout: float = 120.0,
        http_client: anthropic.DefaultAsyncHttpxClient | None = None,
    ) -> None:
        self.model = model
        self.effort: Effort = effort
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, max_retries=max_retries, timeout=timeout, http_client=http_client
        )

    async def assess(self, request: AssessmentRequest) -> LlmReply:
        fallback = self.model in FALLBACK_MODELS
        try:
            response = await self._client.beta.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=[
                    {"type": "text", "text": request.system},
                    {
                        "type": "text",
                        "text": resume_block(request.resume),
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
                messages=[{"role": "user", "content": request.posting}],
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": VERDICT_SCHEMA},
                },
                fallbacks="default" if fallback else anthropic.omit,
                betas=[FALLBACK_BETA] if fallback else anthropic.omit,
            )
        except anthropic.APIStatusError as exc:
            return LlmReply(
                model=self.model,
                error=f"HTTP {exc.status_code}: {_detail(exc)}",
                fatal=exc.status_code in FATAL_STATUSES,
            )
        except anthropic.APIConnectionError as exc:  # timeouts included
            return LlmReply(model=self.model, error=f"network error ({type(exc).__name__})")
        usage = response.usage
        reply = LlmReply(
            model=response.model,
            input_tokens=usage.input_tokens
            + (usage.cache_read_input_tokens or 0)
            + (usage.cache_creation_input_tokens or 0),
            output_tokens=usage.output_tokens,
        )
        if response.stop_reason == "refusal":
            return dataclasses.replace(reply, error="the model refused to assess this posting")
        if response.stop_reason == "max_tokens":
            error = f"the answer was cut off at max_tokens ({MAX_TOKENS})"
            return dataclasses.replace(reply, error=error)
        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            error = f"no answer (stop_reason {response.stop_reason})"
            return dataclasses.replace(reply, error=error)
        try:
            verdict = Verdict.model_validate_json(text)
        except ValidationError:
            return dataclasses.replace(reply, error="the answer didn't match the schema")
        return dataclasses.replace(
            reply,
            assessment=Assessment(
                level=verdict.level,
                backend_focus=_share(verdict.backend_focus),
                fit=_share(verdict.fit),
                reason=verdict.reason.strip(),
            ),
        )

    async def aclose(self) -> None:
        await self._client.close()
