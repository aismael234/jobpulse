import math
from array import array

import pytest

from jobpulse.core.similarity import (
    DEFAULT_ANCHORS,
    RawMatch,
    ResumeVectors,
    anchors,
    calibrate,
    card_line,
    chunk_text,
    dot,
    match,
    rescore_breakdown,
    resume_chunks,
    resume_lines,
    resume_raw,
    similarities,
)


def vec(*values: float) -> array[float]:
    norm = math.sqrt(sum(v * v for v in values))
    return array("f", (v / norm for v in values))


# chunk_text


def test_short_text_is_one_chunk() -> None:
    assert chunk_text("Build Python services.\n\nShip often.") == [
        "Build Python services.\n\nShip often."
    ]


def test_empty_text_has_no_chunks() -> None:
    assert chunk_text("   \n\n ") == []


def test_chunks_break_between_paragraphs_and_stay_under_the_size() -> None:
    paragraphs = [f"Paragraph {i} " + "word " * 60 for i in range(10)]  # ~320 chars each
    chunks = chunk_text("\n\n".join(paragraphs), size=1000, overlap=0)
    assert len(chunks) > 1
    assert all(len(chunk) <= 1000 for chunk in chunks)
    joined = "\n\n".join(chunks)
    for paragraph in paragraphs:
        assert paragraph.strip() in joined  # no paragraph is cut when it fits


def test_chunks_overlap_by_the_last_paragraph() -> None:
    paragraphs = [f"P{i} " + "x" * 300 for i in range(6)]
    chunks = chunk_text("\n\n".join(paragraphs), size=1000, overlap=400)
    assert len(chunks) >= 2
    last_of_first = chunks[0].split("\n\n")[-1]
    assert chunks[1].startswith(last_of_first)


def test_a_paragraph_longer_than_the_size_is_split_at_sentences() -> None:
    long = " ".join(f"Sentence number {i} says something useful." for i in range(80))
    chunks = chunk_text(long, size=500, overlap=0)
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert all(chunk.endswith(".") for chunk in chunks)


def test_at_most_max_chunks() -> None:
    text = "\n\n".join("y" * 900 for _ in range(30))
    assert len(chunk_text(text, size=1000, overlap=0, max_chunks=8)) == 8


# resume_lines

RESUME = """Jane Doe
555-123-4567 | jane@example.com | linkedin.com/in/janedoe
U.S. Citizen in Springfield, IL
Education
● B.S. in Computer Science from Example University
Work History
Software Engineer, Acme, CA November 2024 to October 2026
● Built internal tools in React and TypeScript to help the team find causes of slow
requests in production services.
● Owned PostgreSQL migrations.
Skills
Languages: Python, TypeScript, Go, SQL, and a little Rust for side projects
"""


def test_resume_lines_keep_bullets_and_join_wrapped_lines() -> None:
    lines = resume_lines(RESUME)
    assert (
        "Built internal tools in React and TypeScript to help the team find causes of slow "
        "requests in production services."
    ) in lines
    assert "Owned PostgreSQL migrations." in lines
    assert "B.S. in Computer Science from Example University" in lines


def test_resume_lines_drop_headings_contact_and_job_headers() -> None:
    lines = resume_lines(RESUME)
    joined = "\n".join(lines)
    for gone in ("Jane Doe", "555", "jane@example.com", "linkedin", "Citizen", "Work History"):
        assert gone not in joined
    assert not any("November 2024" in line for line in lines)


def test_resume_lines_keep_long_plain_lines() -> None:
    assert "Languages: Python, TypeScript, Go, SQL, and a little Rust for side projects" in (
        resume_lines(RESUME)
    )


def test_resume_lines_never_keep_a_contact_detail_inside_a_bullet() -> None:
    lines = resume_lines("● Reach me at jane@example.com or 555-123-4567 any time you like")
    assert lines == []


def test_resume_lines_of_plain_text_without_bullets() -> None:
    text = "Built a payments service in Go that handles a million requests a day.\nShort line\n"
    assert resume_lines(text) == [
        "Built a payments service in Go that handles a million requests a day."
    ]


# vectors and matching


def test_dot() -> None:
    assert dot(vec(1, 0), vec(1, 0)) == pytest.approx(1.0)
    assert dot(vec(1, 0), vec(0, 1)) == pytest.approx(0.0)


def test_similarities_is_posting_by_resume() -> None:
    grid = similarities([vec(1, 0), vec(0, 1)], [vec(1, 0), vec(1, 1), vec(0, 1)])
    assert len(grid) == 2
    assert [round(v, 3) for v in grid[0]] == [1.0, 0.707, 0.0]


POSTING = [vec(1, 0, 0), vec(0, 1, 0)]
RESUME_VECTORS = [vec(1, 0, 0), vec(0, 0, 1), vec(0, 1, 1)]


def test_match_max_takes_the_best_pair_and_its_line() -> None:
    assert match(POSTING, RESUME_VECTORS, "max") == RawMatch(pytest.approx(1.0), 0)


def test_match_top3_averages_the_three_best_pairs() -> None:
    result = match(POSTING, RESUME_VECTORS, "top3")
    assert result.raw == pytest.approx((1.0 + 0.7071 + 0.0) / 3, abs=1e-3)
    assert result.line_index == 0


def test_match_per_line_averages_each_lines_best_chunk() -> None:
    result = match(POSTING, RESUME_VECTORS, "per_line")
    assert result.raw == pytest.approx((1.0 + 0.0 + 0.7071) / 3, abs=1e-3)


def test_match_mean_pools_both_sides() -> None:
    result = match(POSTING, RESUME_VECTORS, "mean")
    pooled_posting = vec(1, 1, 0)
    pooled_resume = vec(1 / 1, 0.7071, 1 + 0.7071)
    assert result.raw == pytest.approx(dot(pooled_posting, pooled_resume), abs=1e-3)


def test_match_of_nothing_is_none() -> None:
    assert match([], RESUME_VECTORS, "max") is None
    assert match(POSTING, [], "max") is None


# calibration and re-scoring


def test_anchors_are_the_5th_and_95th_percentiles() -> None:
    values = [i / 100 for i in range(101)]
    low, high = anchors(values)
    assert low == pytest.approx(0.05)
    assert high == pytest.approx(0.95)


def test_calibrate_maps_anchors_to_0_and_1_and_clamps() -> None:
    assert calibrate(0.5, (0.4, 0.6)) == pytest.approx(0.5)
    assert calibrate(0.3, (0.4, 0.6)) == 0.0
    assert calibrate(0.9, (0.4, 0.6)) == 1.0
    assert calibrate(0.5, (0.6, 0.6)) == 0.0  # degenerate anchors


def test_rescore_breakdown_replaces_stack_with_resume() -> None:
    stored = {
        "freshness": 0.2,
        "company_priority": 0.0,
        "title": 0.3,
        "stack": 0.125,
        "level_mismatch": 0.0,
        "applicants": None,
        "llm": 0.05,
        "total": 0.675,
    }
    total, breakdown = rescore_breakdown(stored, resume=0.8, weight=0.25)
    assert breakdown["stack"] is None
    assert breakdown["resume"] == pytest.approx(0.2)
    assert total == pytest.approx(0.2 + 0.3 + 0.2 + 0.05)
    assert breakdown["total"] == pytest.approx(total)
    assert stored["stack"] == 0.125  # the stored breakdown is left alone


# M5b: the production match


def test_resume_raw_scores_against_the_chunks_and_names_the_closest_line() -> None:
    """The score is M5a's best variant (the description against the mean resume chunk); the
    line shown is the single closest resume line."""
    resume = ResumeVectors(
        lines=(vec(1, 0, 0), vec(0, 1, 0), vec(0, 0, 1)), chunks=(vec(1, 1, 0), vec(0, 0, 1))
    )
    posting = [vec(0, 1, 0.2)]
    found = resume_raw(posting, resume)
    assert found is not None
    assert found.line_index == 1
    expected = dot(vec(0, 1, 0.2), vec(1 / math.sqrt(2), 1 / math.sqrt(2), 1))
    assert found.raw == pytest.approx(expected, abs=1e-4)


def test_resume_raw_of_nothing_is_none() -> None:
    assert resume_raw([], ResumeVectors(lines=(vec(1, 0),), chunks=(vec(1, 0),))) is None
    assert resume_raw([vec(1, 0)], ResumeVectors(lines=(), chunks=())) is None


def test_resume_chunks_join_the_lines() -> None:
    lines = ["Built Python services on Postgres", "Led a React rewrite of the billing app"]
    assert resume_chunks(lines) == ["\n".join(lines)]
    assert resume_chunks([]) == []


def test_card_line_is_cut_at_160_characters() -> None:
    assert card_line("Built Python services") == "Built Python services"
    long = "word " * 60
    cut = card_line(long)
    assert len(cut) <= 160
    assert cut.endswith("…")
    assert not cut.removesuffix("…").endswith(" ")


def test_default_anchors_are_m5as_measurement() -> None:
    assert DEFAULT_ANCHORS == (0.668, 0.776)
