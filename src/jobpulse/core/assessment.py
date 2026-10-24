"""The LLM layer's second opinion, as pure functions: which rule decisions sit close enough to
the instant threshold to ask about, and how an answer moves the score.

An assessment says what level the role really hires for, how much of it is backend work, and
how well the resume fits it. The two scores average into a signal in [0, 1] (0 when the level
isn't one you want), and the signal moves the rule score by up to ``weight`` either way:
``score + weight·(2·signal - 1)``.
"""

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from jobpulse.core.models import Decision

Level = Literal["intern", "entry", "mid", "senior", "staff", "principal", "manager", "unknown"]
LEVELS: tuple[Level, ...] = (
    "intern",
    "entry",
    "mid",
    "senior",
    "staff",
    "principal",
    "manager",
    "unknown",
)


@dataclass(frozen=True, slots=True)
class Assessment:
    level: Level
    backend_focus: float  # 0..1
    fit: float  # 0..1
    reason: str = ""


# Scores are rounded to 4 decimals, so this only absorbs float error at the band's edges.
_EDGE = 1e-9
_MAX_REASON_CHARS = 160
_MATCH_TIERS = frozenset({"instant", "digest"})


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _rounded(value: float) -> float:
    return round(value, 4) + 0.0  # + 0.0 turns -0.0 into 0.0


def _unwanted(assessment: Assessment, wanted_levels: Collection[str]) -> bool:
    return (
        bool(wanted_levels)
        and assessment.level != "unknown"
        and (assessment.level not in wanted_levels)
    )


def _shorten(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= _MAX_REASON_CHARS:
        return text
    return text[: _MAX_REASON_CHARS - 1].rstrip() + "…"


def needs_assessment(decision: Decision, *, threshold: float, band: float) -> bool:
    """A match (instant or digest) whose rule score is within ``band`` of ``threshold``."""
    if decision.tier not in _MATCH_TIERS or decision.score is None:
        return False
    return abs(decision.score - threshold) <= band + _EDGE


def assessment_signal(assessment: Assessment, wanted_levels: Collection[str] = ()) -> float:
    """The mean of backend focus and fit; 0.0 when the level is known and not wanted."""
    if _unwanted(assessment, wanted_levels):
        return 0.0
    return _clamp((assessment.backend_focus + assessment.fit) / 2)


def apply_assessment(
    decision: Decision,
    assessment: Assessment,
    *,
    weight: float,
    threshold: float,
    wanted_levels: Collection[str] = (),
) -> Decision:
    """The decision after the second opinion: score, tier, breakdown (``llm``) and reason."""
    if decision.score is None:
        return decision
    delta = _rounded(weight * (2 * assessment_signal(assessment, wanted_levels) - 1))
    total = _rounded(decision.score + delta)
    instant = total >= threshold
    opinion = (
        f"llm {delta:+.2f}: {assessment.level}, backend {assessment.backend_focus:.0%}, "
        f"fit {assessment.fit:.0%}"
    )
    if _unwanted(assessment, wanted_levels):
        opinion += f" (level {assessment.level} isn't in llm.wanted_levels)"
    if assessment.reason.strip():
        opinion += f" ({_shorten(assessment.reason)})"
    _, _, notes = decision.reason.partition("; ")
    verdict = f"score {total:.2f} {'≥' if instant else '<'} {threshold:.2f}"
    return Decision(
        tier="instant" if instant else "digest",
        reason="; ".join(part for part in (verdict, opinion, notes) if part),
        score=total,
        breakdown={**(decision.breakdown or {}), "llm": delta, "total": total},
    )
