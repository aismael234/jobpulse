from datetime import timedelta

import pytest

from jobpulse.core.applicants import ApplicantCount
from jobpulse.core.scoring import (
    ScoreInputs,
    Weights,
    applicant_component,
    freshness_component,
    priority_component,
    score,
    with_resume,
)

WEIGHTS = Weights(
    freshness=0.25, company_priority=0.2, title=0.3, stack=0.25, level_mismatch=0.5, applicants=0.1
)


def test_score_is_the_weighted_sum() -> None:
    total, breakdown = score(
        ScoreInputs(freshness=1.0, company_priority=0.5, title=1.0, stack=0.5), WEIGHTS
    )
    assert total == pytest.approx(0.25 + 0.1 + 0.3 + 0.125)
    assert breakdown["freshness"] == pytest.approx(0.25)
    assert breakdown["company_priority"] == pytest.approx(0.1)
    assert breakdown["title"] == pytest.approx(0.3)
    assert breakdown["stack"] == pytest.approx(0.125)
    assert breakdown["level_mismatch"] == 0.0
    assert breakdown["applicants"] is None
    assert breakdown["total"] == pytest.approx(total)


def test_level_mismatch_subtracts() -> None:
    clean, _ = score(ScoreInputs(freshness=1.0, company_priority=0.0, title=1.0), WEIGHTS)
    penalized, breakdown = score(
        ScoreInputs(freshness=1.0, company_priority=0.0, title=1.0, level_mismatch=1.0), WEIGHTS
    )
    assert penalized == pytest.approx(clean - 0.5)
    assert breakdown["level_mismatch"] == pytest.approx(-0.5)


def test_unknown_stack_and_applicants_contribute_nothing() -> None:
    total, breakdown = score(ScoreInputs(freshness=0.0, company_priority=0.0, title=1.0), WEIGHTS)
    assert total == pytest.approx(0.3)
    assert breakdown["stack"] is None
    assert breakdown["applicants"] is None


def test_known_applicant_signal_counts() -> None:
    total, breakdown = score(
        ScoreInputs(freshness=0.0, company_priority=0.0, title=0.0, applicants=1.0), WEIGHTS
    )
    assert total == pytest.approx(0.1)
    assert breakdown["applicants"] == pytest.approx(0.1)


@pytest.mark.parametrize(
    ("age_hours", "expected"), [(0, 1.0), (24, 0.5), (48, 0.0), (60, 0.0), (-2, 1.0)]
)
def test_freshness_component(age_hours: float, expected: float) -> None:
    assert freshness_component(timedelta(hours=age_hours), timedelta(hours=48)) == pytest.approx(
        expected
    )


@pytest.mark.parametrize(("priority", "expected"), [(0, 0.0), (5, 0.5), (10, 1.0), (20, 1.0)])
def test_priority_component(priority: int, expected: float) -> None:
    assert priority_component(priority, 10) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (ApplicantCount("Be among the first 25 applicants", None, 25), 0.875),
        (ApplicantCount("1 applicant", 1, 1), 0.995),
        (ApplicantCount("87 applicants", 87, 87), 0.565),
        (ApplicantCount("Over 100 applicants", 100, None), 0.5),
        (ApplicantCount("Over 200 applicants", 200, None), 0.0),
        (ApplicantCount("1,204 applicants", 1204, 1204), 0.0),
    ],
)
def test_applicant_component_falls_with_the_count(count: ApplicantCount, expected: float) -> None:
    """A range counts from its known end: "first 25" from 25, "Over 100" from 100."""
    assert applicant_component(count, 200) == pytest.approx(expected)


def test_applicant_component_scales_against_the_cap() -> None:
    assert applicant_component(ApplicantCount("50 applicants", 50, 50), 100) == pytest.approx(0.5)


def test_a_known_resume_part_replaces_the_stack_part() -> None:
    weights = Weights(stack=0.25, resume=0.2)
    total, breakdown = score(
        ScoreInputs(freshness=0.0, company_priority=0.0, title=1.0, stack=1.0, resume=0.5),
        weights,
    )
    assert total == pytest.approx(0.3 + 0.1)
    assert breakdown["stack"] is None
    assert breakdown["resume"] == pytest.approx(0.1)


def test_without_a_resume_part_the_breakdown_has_no_resume_key() -> None:
    _, breakdown = score(ScoreInputs(freshness=0.0, company_priority=0.0, title=1.0), WEIGHTS)
    assert "resume" not in breakdown


def test_with_resume_swaps_the_parts_and_keeps_the_rest() -> None:
    stored = {"title": 0.3, "stack": 0.25, "llm": -0.05, "applicants": None, "total": 0.5}
    total, breakdown = with_resume(stored, resume=0.1, stack=None)
    assert total == pytest.approx(0.35)
    assert breakdown == {
        "title": 0.3,
        "stack": None,
        "llm": -0.05,
        "applicants": None,
        "resume": 0.1,
        "total": 0.35,
    }

    total, restored = with_resume(breakdown, resume=None, stack=0.2)
    assert total == pytest.approx(0.45)
    assert "resume" not in restored
    assert restored["stack"] == pytest.approx(0.2)
