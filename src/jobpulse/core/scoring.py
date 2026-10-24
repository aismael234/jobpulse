"""Score = w_fresh·freshness + w_prio·company_priority + w_title·title_match
+ w_stack·stack_match - w_level·level_mismatch + w_appl·applicant_signal.

Every component is in [0, 1]. Stack and applicant terms count only when known. The LLM layer
(``core.assessment``) may add a last term, ``w_llm·(2·signal - 1)``, for postings near the
instant threshold.
"""

from dataclasses import dataclass
from datetime import timedelta

from jobpulse.core.applicants import ApplicantCount


@dataclass(frozen=True, slots=True)
class Weights:
    freshness: float = 0.25
    company_priority: float = 0.20
    title: float = 0.30
    stack: float = 0.25
    level_mismatch: float = 0.50
    applicants: float = 0.10
    llm: float = 0.15  # the most the LLM layer's second opinion adds or subtracts


@dataclass(frozen=True, slots=True)
class ScoreInputs:
    freshness: float
    company_priority: float
    title: float
    stack: float | None = None
    level_mismatch: float = 0.0
    applicants: float | None = None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def freshness_component(age: timedelta, window: timedelta) -> float:
    """1.0 for a brand-new posting, falling linearly to 0.0 at the end of the window."""
    if window <= timedelta(0):
        return 0.0
    return _clamp(1.0 - age / window)


def priority_component(priority: int, cap: int) -> float:
    return _clamp(priority / cap) if cap > 0 else 0.0


def applicant_component(count: ApplicantCount, cap: int) -> float:
    """1.0 for no applicants, falling linearly to 0.0 at ``cap``. A range counts from its known
    end: "Be among the first 25" from 25, "Over 200" from 200."""
    estimate = count.minimum if count.minimum is not None else count.maximum
    if estimate is None or cap <= 0:
        return 0.0
    return _clamp(1.0 - estimate / cap)


def _rounded(value: float) -> float:
    return round(value, 4) + 0.0  # + 0.0 turns -0.0 into 0.0


def score(inputs: ScoreInputs, weights: Weights) -> tuple[float, dict[str, float | None]]:
    """The total and a per-component breakdown (``None`` for unknown components)."""
    parts: dict[str, float | None] = {
        "freshness": weights.freshness * inputs.freshness,
        "company_priority": weights.company_priority * inputs.company_priority,
        "title": weights.title * inputs.title,
        "stack": None if inputs.stack is None else weights.stack * inputs.stack,
        "level_mismatch": -weights.level_mismatch * inputs.level_mismatch,
        "applicants": None if inputs.applicants is None else weights.applicants * inputs.applicants,
    }
    total = sum(value for value in parts.values() if value is not None)
    breakdown: dict[str, float | None] = {
        name: None if value is None else _rounded(value) for name, value in parts.items()
    }
    breakdown["total"] = _rounded(total)
    return _rounded(total), breakdown
