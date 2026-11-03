"""Resume-to-posting similarity over embedding vectors (pure: the vectors come in).

Vectors are L2-normalized ``array('f')``, so a dot product is the cosine similarity. A
posting is embedded as a few chunks of its text, the resume as its lines (``resume_lines``,
which leaves out headings and contact details, so the line shown on a card is never personal
information). ``match`` reduces the posting-by-resume similarity grid to one raw number and
the closest line; ``calibrate`` turns raw numbers into [0, 1] against anchors measured on
recent matches, because raw cosines from one model sit in a narrow band.

Scoring (M5b) uses M5a's best variant: the description embedded as one block against the mean
of the resume's chunks (``resume_raw``). The line shown on a card is the single closest resume
line, a separate and cheap lookup.
"""

import math
import re
from array import array
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from jobpulse.core.scoring import with_resume

type Vector = array[float]
type Aggregation = Literal["mean", "max", "top3", "per_line"]

AGGREGATIONS: tuple[Aggregation, ...] = ("mean", "max", "top3", "per_line")

_BLANK_LINE = re.compile(r"\n\s*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_BULLET = re.compile(r"^\s*[●•▪‣◦·*\-–]\s*")
_EMAIL = re.compile(r"\S+@\S+\.\w+")
_PHONE = re.compile(r"(?:\+?\d{1,2}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]\d{4}\b")
_URL = re.compile(r"https?://|www\.|\b(?:linkedin|github)\.com\b|\.\w{2,4}/\S", re.IGNORECASE)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_CONNECTIVE_END = re.compile(
    r"(?:[,;:/&(-]|\b(?:a|an|and|as|at|by|for|from|in|into|of|on|or|the|to|with))$", re.IGNORECASE
)
_MIN_BULLET_WORDS = 3
_MIN_PLAIN_WORDS = 8
CARD_LINE_CHARS = 160

# p5 and p95 of raw matches over 800 recent matches, measured in M5a (2026-11-01) with the
# quantized bge-small-en-v1.5. Used until an install has enough matches to measure its own.
DEFAULT_ANCHORS: tuple[float, float] = (0.668, 0.776)
MIN_ANCHOR_SAMPLES = 50


@dataclass(frozen=True, slots=True)
class RawMatch:
    raw: float  # uncalibrated similarity
    line_index: int  # the resume line closest to the posting


@dataclass(frozen=True, slots=True)
class ResumeVectors:
    lines: tuple[Vector, ...]  # one per ``resume_lines`` line
    chunks: tuple[Vector, ...]  # one per ``resume_chunks`` chunk


@dataclass(frozen=True, slots=True)
class ResumeMatch:
    """A posting's resume similarity as the score and the dashboard use it."""

    score: float  # calibrated, in [0, 1]
    line: str  # the closest resume line, cut for a card


# Chunking


def _split_long(paragraph: str, size: int) -> list[str]:
    """A paragraph longer than ``size`` as pieces that fit, broken between sentences (and
    mid-sentence only when one sentence alone is too long)."""
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(paragraph):
        while len(sentence) > size:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:size])
            sentence = sentence[size:].lstrip()
        if not sentence:
            continue
        candidate = f"{current} {sentence}" if current else sentence
        if len(candidate) <= size:
            current = candidate
        else:
            pieces.append(current)
            current = sentence
    if current:
        pieces.append(current)
    return pieces


def chunk_text(
    text: str, *, size: int = 1500, overlap: int = 200, max_chunks: int = 8
) -> list[str]:
    """``text`` as at most ``max_chunks`` chunks of at most ``size`` characters, broken between
    paragraphs. Each chunk after the first starts with the previous one's last paragraphs, up
    to ``overlap`` characters, so a requirement split across a break is still read whole.

    Paragraphs are separated by blank lines, or by line breaks when there are none (stored
    descriptions have one line per block)."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    separator = "\n\n" if _BLANK_LINE.search(text) else "\n"
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n" if separator == "\n\n" else "\n", text)]
    pieces = [piece for p in paragraphs if p for piece in _split_long(p, size)]

    chunks: list[str] = []
    current: list[str] = []
    fresh = False  # whether ``current`` holds anything beyond the carried overlap
    for piece in pieces:
        if current and len(separator.join([*current, piece])) > size:
            chunks.append(separator.join(current))
            if len(chunks) == max_chunks:
                return chunks
            carried: list[str] = []
            for previous in reversed(current):
                if len(separator.join([previous, *carried])) > overlap:
                    break
                carried.insert(0, previous)
            if carried and len(separator.join([*carried, piece])) > size:
                carried = []
            current = carried
        current.append(piece)
        fresh = True
    if current and fresh:
        chunks.append(separator.join(current))
    return chunks[:max_chunks]


# Resume lines


def _is_contact(line: str) -> bool:
    return bool(_EMAIL.search(line) or _PHONE.search(line) or _URL.search(line))


def _continues(line: str, previous: str) -> bool:
    """Whether ``line`` is the wrapped rest of ``previous`` (a PDF breaks lines mid-sentence)."""
    return line[:1].islower() or bool(_CONNECTIVE_END.search(previous))


def resume_chunks(lines: Sequence[str]) -> list[str]:
    """The resume's content lines as chunks: the resume side that M5a scored best."""
    return chunk_text("\n".join(lines))


def card_line(line: str, limit: int = CARD_LINE_CHARS) -> str:
    """``line`` cut to at most ``limit`` characters at a word break, with an ellipsis."""
    line = " ".join(line.split())
    if len(line) <= limit:
        return line
    cut = line[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:-") + "…"


def resume_lines(text: str) -> list[str]:
    """The resume's content lines: each bullet (with its wrapped lines joined) and each plain
    line of at least eight words. Headings, the name, contact lines and job headers (a plain
    line with a year) are left out, and so is any line holding an email, phone or URL."""
    items: list[tuple[bool, str]] = []  # (is a bullet, text)
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        bullet = _BULLET.match(line)
        if bullet:
            items.append((True, line[bullet.end() :]))
        elif items and items[-1][0] and _continues(line, items[-1][1]):
            previous = items[-1][1]
            joined = previous + line if previous.endswith("-") else f"{previous} {line}"
            items[-1] = (True, joined)
        else:
            items.append((False, line))
    lines: list[str] = []
    for is_bullet, item in items:
        words = len(item.split())
        if _is_contact(item):
            continue
        if (is_bullet and words >= _MIN_BULLET_WORDS) or (
            not is_bullet and words >= _MIN_PLAIN_WORDS and not _YEAR.search(item)
        ):
            lines.append(item)
    return lines


# Vectors


def dot(a: Vector, b: Vector) -> float:
    return math.fsum(x * y for x, y in zip(a, b, strict=True))


def similarities(posting: Sequence[Vector], resume: Sequence[Vector]) -> list[list[float]]:
    """The cosine of every (posting chunk, resume line) pair: one row per posting chunk."""
    return [[dot(chunk, line) for line in resume] for chunk in posting]


def _pooled(vectors: Sequence[Vector]) -> Vector:
    total = [math.fsum(column) for column in zip(*vectors, strict=True)]
    norm = math.sqrt(math.fsum(v * v for v in total)) or 1.0
    return array("f", (v / norm for v in total))


def match(
    posting: Sequence[Vector], resume: Sequence[Vector], aggregation: Aggregation
) -> RawMatch | None:
    """How close a posting is to the resume, and the resume line closest to it. ``None``
    when either side has no vectors.

    - ``mean``: the mean posting chunk against the mean resume line;
    - ``max``: the single closest pair;
    - ``top3``: the mean of the three closest pairs;
    - ``per_line``: each resume line's closest chunk, averaged over the lines."""
    if not posting or not resume:
        return None
    grid = similarities(posting, resume)
    best_per_line = [max(row[j] for row in grid) for j in range(len(resume))]
    line_index = max(range(len(resume)), key=best_per_line.__getitem__)
    if aggregation == "mean":
        raw = dot(_pooled(posting), _pooled(resume))
    elif aggregation == "max":
        raw = best_per_line[line_index]
    elif aggregation == "top3":
        pairs = sorted((value for row in grid for value in row), reverse=True)[:3]
        raw = math.fsum(pairs) / len(pairs)
    else:
        raw = math.fsum(best_per_line) / len(best_per_line)
    return RawMatch(raw, line_index)


def resume_raw(posting: Sequence[Vector], resume: ResumeVectors) -> RawMatch | None:
    """The production match: the posting against the mean resume chunk, and the index of the
    closest resume line. ``None`` when either side has no vectors."""
    scored = match(posting, resume.chunks, "mean")
    closest = match(posting, resume.lines, "max")
    if scored is None or closest is None:
        return None
    return RawMatch(scored.raw, closest.line_index)


# Calibration and scores


def _percentile(ordered: Sequence[float], q: float) -> float:
    position = q * (len(ordered) - 1)
    below = math.floor(position)
    above = min(below + 1, len(ordered) - 1)
    return ordered[below] + (ordered[above] - ordered[below]) * (position - below)


def anchors(values: Sequence[float], low: float = 0.05, high: float = 0.95) -> tuple[float, float]:
    """The ``low`` and ``high`` percentiles of raw matches (linear interpolation)."""
    if not values:
        raise ValueError("anchors need at least one value")
    ordered = sorted(values)
    return _percentile(ordered, low), _percentile(ordered, high)


def calibrate(raw: float, bounds: tuple[float, float]) -> float:
    """``raw`` placed between the anchors: 0.0 at or below the low one, 1.0 at or above the
    high one."""
    low, high = bounds
    if high <= low:
        return 0.0
    return max(0.0, min(1.0, (raw - low) / (high - low)))


def rescore_breakdown(
    breakdown: Mapping[str, float | None], *, resume: float, weight: float
) -> tuple[float, dict[str, float | None]]:
    """A stored score breakdown with the resume part (``weight`` times the calibrated ``resume``) in
    place of the stack part. Other parts, the LLM's included, are kept."""
    return with_resume(breakdown, resume=weight * resume, stack=None)
