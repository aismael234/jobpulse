"""M4: the LLM layer's second opinion as pure functions: the band, the signal, the new score."""

from dataclasses import replace

import pytest

from jobpulse.core.assessment import (
    Assessment,
    apply_assessment,
    assessment_signal,
    needs_assessment,
)
from jobpulse.core.models import Decision

THRESHOLD = 0.55
BAND = 0.10


def match(score: float, *, reason: str = "") -> Decision:
    return Decision(
        tier="instant",
        reason=reason or f"score {score:.2f}",
        score=score,
        breakdown={"freshness": 0.25, "title": 0.3, "total": score},
    )


@pytest.mark.parametrize(
    ("score", "asked"),
    [
        (0.45, True),
        (0.50, True),
        (0.55, True),
        (0.649, True),
        (0.65, True),
        (0.44, False),
        (0.66, False),
    ],
)
def test_only_scores_within_the_band_are_asked_about(score: float, asked: bool) -> None:
    assert needs_assessment(match(score), threshold=THRESHOLD, band=BAND) is asked


@pytest.mark.parametrize("tier", ["filtered", "stale", "merged", "baseline", "pending"])
def test_only_matches_are_asked_about(tier: str) -> None:
    decision = Decision(tier=tier, reason="", score=0.55)  # type: ignore[arg-type]
    assert not needs_assessment(decision, threshold=THRESHOLD, band=BAND)


def test_a_match_without_a_score_is_not_asked_about() -> None:
    decision = Decision(tier="instant", reason="")
    assert not needs_assessment(decision, threshold=THRESHOLD, band=BAND)


def test_the_signal_is_the_mean_of_backend_focus_and_fit() -> None:
    assert assessment_signal(Assessment("mid", 0.9, 0.5)) == pytest.approx(0.7)


def test_an_unwanted_level_zeroes_the_signal() -> None:
    assessment = Assessment("staff", 1.0, 1.0)
    assert assessment_signal(assessment, wanted_levels={"entry", "mid"}) == 0.0
    assert assessment_signal(assessment, wanted_levels=()) == 1.0  # no wanted levels: any


def test_an_unknown_level_never_counts_as_unwanted() -> None:
    assert assessment_signal(Assessment("unknown", 0.8, 0.6), wanted_levels={"mid"}) == (
        pytest.approx(0.7)
    )


def test_a_strong_answer_lifts_the_score() -> None:
    decided = apply_assessment(match(0.50), Assessment("mid", 0.9, 0.9, "Python APIs"), weight=0.15)
    assert decided.tier == "instant"
    assert decided.score == pytest.approx(0.50 + 0.15 * 0.8)
    assert decided.breakdown is not None
    assert decided.breakdown["llm"] == pytest.approx(0.12)
    assert decided.breakdown["total"] == pytest.approx(0.62)
    assert decided.breakdown["title"] == 0.3  # the rule components stay
    assert decided.reason.startswith("score 0.62; llm +0.12")
    assert "llm +0.12" in decided.reason
    assert "mid, backend 90%, fit 90%" in decided.reason
    assert "Python APIs" in decided.reason


def test_the_years_asked_survive_the_second_opinion() -> None:
    decision = replace(match(0.50), years_required=3)
    decided = apply_assessment(decision, Assessment("mid", 0.9, 0.9), weight=0.15)
    assert decided.years_required == 3


def test_a_weak_answer_lowers_the_score_but_keeps_the_match() -> None:
    decided = apply_assessment(match(0.60), Assessment("mid", 0.2, 0.2), weight=0.15)
    assert decided.tier == "instant"  # every match is pushed, whatever its score
    assert decided.score == pytest.approx(0.60 - 0.15 * 0.6)
    assert decided.reason.startswith("score 0.51; llm -0.09")


def test_an_unwanted_level_costs_the_whole_weight_and_says_why() -> None:
    decided = apply_assessment(
        match(0.60),
        Assessment("senior", 1.0, 1.0),
        weight=0.15,
        wanted_levels={"entry", "mid"},
    )
    assert decided.score == pytest.approx(0.45)
    assert decided.tier == "instant"
    assert "senior" in decided.reason
    assert "llm.wanted_levels" in decided.reason


def test_rule_notes_in_the_reason_are_kept() -> None:
    decision = match(0.52, reason="score 0.52; asks for 7+ years (max 5)")
    decided = apply_assessment(decision, Assessment("mid", 0.5, 0.5), weight=0.15)
    assert decided.reason.endswith("asks for 7+ years (max 5)")
    assert decided.reason.count("score ") == 1


def test_a_long_llm_reason_is_cut() -> None:
    decided = apply_assessment(match(0.55), Assessment("mid", 0.5, 0.5, "x" * 1000), weight=0.15)
    assert len(decided.reason) < 400
