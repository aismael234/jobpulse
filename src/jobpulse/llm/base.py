"""The provider interface: one request about one posting, one reply. A provider never raises
for an API problem; the reply carries the error instead."""

from dataclasses import dataclass
from typing import Protocol

from jobpulse.core.assessment import Assessment


@dataclass(frozen=True, slots=True)
class AssessmentRequest:
    system: str  # the instructions; the same for every posting
    resume: str  # the candidate's resume; the same for every posting
    posting: str  # the posting as text
    input_hash: str  # names this exact question, for the per-posting cache


@dataclass(frozen=True, slots=True)
class LlmReply:
    model: str  # the model that answered (a refusal fallback may differ from the one asked)
    assessment: Assessment | None = None
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    fatal: bool = False  # the key or the account is the problem: stop asking until a restart
    asked: bool = True  # False when no call was made (asking had stopped): nothing to count


class LlmClient(Protocol):
    model: str

    async def assess(self, request: AssessmentRequest) -> LlmReply: ...

    async def aclose(self) -> None: ...
