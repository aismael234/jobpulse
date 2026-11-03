"""Resume similarity over the database (M5b), shared by the poller and `jobpulse resume`.

A match's resume part replaces its stack part while a resume is stored and the match has an
embedded description. Re-scoring covers open, untriaged matches (New and Saved); Applied and
Dismissed postings keep the score they were triaged with. Everything here is synchronous: the
poller runs the embedding in a thread and calls the database helpers between awaits.
"""

import hashlib
import logging
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.orm import Session

from jobpulse.core.filters import FilterRules, check_description
from jobpulse.core.scoring import Weights, with_resume
from jobpulse.core.similarity import (
    DEFAULT_ANCHORS,
    MIN_ANCHOR_SAMPLES,
    RawMatch,
    ResumeMatch,
    ResumeVectors,
    anchors,
    calibrate,
    card_line,
    resume_chunks,
    resume_raw,
)
from jobpulse.db import resume as resume_db
from jobpulse.db.resume import StoredResume
from jobpulse.db.tables import PostingRow, PostingVector
from jobpulse.embeddings.base import Embedder

log = logging.getLogger(__name__)

SCOPE_STATUSES = ("New", "Saved")
EMBED_BATCH = 64


@dataclass(frozen=True, slots=True)
class RescoreReport:
    rescored: int  # matches scored against the resume
    restored: int  # matches put back on the stack keywords (no vector for them)
    measured_on: int  # matches the anchors were measured on (0: the defaults)


def posting_text(description: str | None) -> str:
    """What a posting is embedded as: its description alone (M5a's best variant)."""
    return (description or "").strip()


def input_hash(model: str, text: str) -> str:
    return hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()


def in_scope() -> list[ColumnElement[bool]]:
    """Open, untriaged matches: the postings a resume change re-scores."""
    return [
        PostingRow.tier == "instant",
        PostingRow.merged_into_id.is_(None),
        PostingRow.closed_at.is_(None),
        PostingRow.status.in_(SCOPE_STATUSES),
    ]


def embed_resume(embedder: Embedder, lines: Sequence[str]) -> ResumeVectors:
    chunks = resume_chunks(lines)
    vectors = embedder.embed([*lines, *chunks])
    return ResumeVectors(lines=tuple(vectors[: len(lines)]), chunks=tuple(vectors[len(lines) :]))


def refresh_resume(session: Session, embedder: Embedder) -> bool:
    """Embed the stored resume with ``embedder`` when its vectors are missing or from another
    model; whether it did."""
    stored = resume_db.get(session)
    if stored is None or not stored.lines or stored.embed_model == embedder.model:
        return False
    resume_db.save_vectors(
        session, model=embedder.model, vectors=embed_resume(embedder, stored.lines)
    )
    return True


def resume_match(
    vectors: Sequence[Any], stored: StoredResume, bounds: tuple[float, float] | None = None
) -> ResumeMatch | None:
    """A posting's vectors against the stored resume, calibrated with ``bounds`` (the stored
    anchors by default)."""
    if stored.vectors is None:
        return None
    found = resume_raw(vectors, stored.vectors)
    return _calibrated(found, stored, bounds or stored.anchors) if found else None


def _calibrated(found: RawMatch, stored: StoredResume, bounds: tuple[float, float]) -> ResumeMatch:
    return ResumeMatch(
        round(calibrate(found.raw, bounds), 4), card_line(stored.lines[found.line_index])
    )


def missing_vectors(session: Session, model: str) -> list[tuple[int, str]]:
    """Open matches with a description but no vector of it from ``model``: (id, text)."""
    rows = session.execute(
        select(PostingRow.id, PostingRow.description_text).where(
            *in_scope(), PostingRow.description_text.is_not(None)
        )
    ).all()
    texts = {row_id: text for row_id, desc in rows if (text := posting_text(desc))}
    have = _hashes(session, texts, model)
    return [
        (row_id, text)
        for row_id, text in texts.items()
        if have.get(row_id) != input_hash(model, text)
    ]


def _hashes(session: Session, ids: Collection[int], model: str) -> dict[int, str]:
    wanted = sorted(ids)
    found: dict[int, str] = {}
    for start in range(0, len(wanted), 500):
        rows = session.execute(
            select(PostingVector.posting_id, PostingVector.input_hash).where(
                PostingVector.posting_id.in_(wanted[start : start + 500]),
                PostingVector.model == model,
            )
        )
        found.update({posting_id: digest for posting_id, digest in rows})
    return found


def backfill(
    session: Session,
    embedder: Embedder,
    *,
    now: datetime,
    batch_size: int = EMBED_BATCH,
    progress: Callable[[int], None] | None = None,
) -> int:
    """Embed every open match's description that has no vector from ``embedder`` yet, in
    batches; ``progress`` gets the running count after each. Returns the postings embedded."""
    model = embedder.model
    missing = missing_vectors(session, model)
    done = 0
    for start in range(0, len(missing), batch_size):
        batch = missing[start : start + batch_size]
        unique = list(dict.fromkeys(text for _, text in batch))
        by_text = dict(zip(unique, embedder.embed(unique), strict=True))
        resume_db.save_posting_vectors(
            session,
            {row_id: (input_hash(model, text), [by_text[text]]) for row_id, text in batch},
            model=model,
            now=now,
        )
        done += len(batch)
        if progress is not None:
            progress(done)
    return done


def count_in_scope_vectors(session: Session, model: str) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(PostingRow)
            .join(PostingVector, PostingVector.posting_id == PostingRow.id)
            .where(*in_scope(), PostingVector.model == model)
        )
        or 0
    )


def needs_rescore(session: Session, stored: StoredResume) -> bool:
    """Whether open matches need re-scoring: the resume (or its model) changed since the last
    re-score, or the anchors are still the defaults and there are now enough matches to
    measure them on."""
    if stored.vectors is None or stored.embed_model is None:
        return False
    if stored.rescored_at is None:
        return True
    return (
        stored.anchor_count == 0
        and count_in_scope_vectors(session, stored.embed_model) >= MIN_ANCHOR_SAMPLES
    )


def _stack_part(description: str | None, rules: FilterRules, weights: Weights) -> float | None:
    stack = check_description(description, rules).stack_match
    return None if stack is None else weights.stack * stack


def rescore(
    session: Session,
    *,
    rules: FilterRules,
    weights: Weights,
    now: datetime,
    min_samples: int = MIN_ANCHOR_SAMPLES,
) -> RescoreReport:
    """Re-score open matches against the stored resume. Anchors are measured on their raw
    matches when there are at least ``min_samples``. A match without a current vector goes
    back to the stack keywords if it had a resume part. Other score parts are kept."""
    stored = resume_db.get(session)
    if stored is None or stored.vectors is None or stored.embed_model is None:
        raise LookupError("no embedded resume is stored")
    model = stored.embed_model
    rows = session.execute(
        select(PostingRow.id, PostingRow.description_text, PostingRow.score_breakdown).where(
            *in_scope(), PostingRow.score_breakdown.is_not(None)
        )
    ).all()
    cached = resume_db.posting_vectors(session, [row.id for row in rows], model=model)
    raws: dict[int, RawMatch] = {}
    for row_id, description, _ in rows:
        text = posting_text(description)
        entry = cached.get(row_id)
        if text and entry is not None and entry[0] == input_hash(model, text):
            found = resume_raw(entry[1], stored.vectors)
            if found is not None:
                raws[row_id] = found
    values = [found.raw for found in raws.values()]
    measured = len(values) >= max(1, min_samples)
    bounds = anchors(values) if measured else DEFAULT_ANCHORS

    updates: list[dict[str, Any]] = []
    restored = 0
    for row_id, description, breakdown in rows:
        found = raws.get(row_id)
        if found is not None:
            match = _calibrated(found, stored, bounds)
            total, parts = with_resume(breakdown, resume=weights.resume * match.score, stack=None)
            values_ = {"resume_score": match.score, "resume_line": match.line}
        elif "resume" in breakdown:
            stack = _stack_part(description, rules, weights)
            total, parts = with_resume(breakdown, resume=None, stack=stack)
            values_ = {"resume_score": None, "resume_line": None}
            restored += 1
        else:
            continue
        updates.append({"id": row_id, "score": total, "score_breakdown": parts, **values_})
    if updates:
        session.execute(update(PostingRow), updates)
    resume_db.mark_rescored(
        session, anchors=bounds if measured else None, count=len(values) if measured else 0, now=now
    )
    log.info(
        "resume: re-scored %d open matches (%d back on stack keywords), anchors %.3f-%.3f%s",
        len(raws),
        restored,
        *bounds,
        f" measured on {len(values)}" if measured else " (defaults)",
    )
    return RescoreReport(len(raws), restored, len(values) if measured else 0)


def restore_stack(session: Session, *, rules: FilterRules, weights: Weights) -> int:
    """Put open matches with a resume part back on the stack keywords (the resume was
    cleared). Returns how many changed."""
    rows = session.execute(
        select(PostingRow.id, PostingRow.description_text, PostingRow.score_breakdown).where(
            *in_scope(), PostingRow.score_breakdown.is_not(None)
        )
    ).all()
    updates: list[dict[str, Any]] = []
    for row_id, description, breakdown in rows:
        if "resume" not in breakdown:
            continue
        stack = _stack_part(description, rules, weights)
        total, parts = with_resume(breakdown, resume=None, stack=stack)
        updates.append(
            {
                "id": row_id,
                "score": total,
                "score_breakdown": parts,
                "resume_score": None,
                "resume_line": None,
            }
        )
    if updates:
        session.execute(update(PostingRow), updates)
    return len(updates)
