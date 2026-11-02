"""The stored resume and the posting vectors (M5b).

Vectors are stored as little-endian float32 blobs, one vector after another, with their
dimension beside them. Like ``repo``, everything here leaves the transaction to the caller.
"""

import hashlib
import sys
from array import array
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from jobpulse.core.similarity import DEFAULT_ANCHORS, ResumeVectors, Vector
from jobpulse.db.tables import PostingVector, ResumeRow

RESUME_ID = 1
_SWAP = sys.byteorder != "little"


@dataclass(frozen=True, slots=True)
class StoredResume:
    filename: str
    media_type: str
    sha256: str
    uploaded_at: datetime
    lines: tuple[str, ...]
    embed_model: str | None  # the model ``vectors`` came from; None until embedded
    vectors: ResumeVectors | None
    anchors: tuple[float, float]  # calibration: measured, else DEFAULT_ANCHORS
    anchor_count: int  # the matches the anchors were measured on (0: the defaults)
    rescored_at: datetime | None  # None: open matches still need re-scoring


def encode(vectors: Sequence[Vector]) -> bytes:
    values = array("f")
    for vector in vectors:
        values.extend(vector)
    if _SWAP:
        values.byteswap()
    return values.tobytes()


def decode(blob: bytes, *, dims: int) -> list[Vector]:
    values = array("f")
    values.frombytes(blob)
    if _SWAP:
        values.byteswap()
    if dims <= 0 or len(values) % dims:
        raise ValueError(f"a blob of {len(values)} floats doesn't hold {dims}-dim vectors")
    return [values[start : start + dims] for start in range(0, len(values), dims)]


def _stored(row: ResumeRow) -> StoredResume:
    vectors = None
    if row.dims and row.line_vectors is not None and row.chunk_vectors is not None:
        vectors = ResumeVectors(
            lines=tuple(decode(row.line_vectors, dims=row.dims)),
            chunks=tuple(decode(row.chunk_vectors, dims=row.dims)),
        )
    anchors = (
        (row.anchor_low, row.anchor_high)
        if row.anchor_low is not None and row.anchor_high is not None
        else DEFAULT_ANCHORS
    )
    return StoredResume(
        filename=row.filename,
        media_type=row.media_type,
        sha256=row.sha256,
        uploaded_at=row.uploaded_at,
        lines=tuple(row.lines),
        embed_model=row.embed_model,
        vectors=vectors,
        anchors=anchors,
        anchor_count=row.anchor_count,
        rescored_at=row.rescored_at,
    )


def get(session: Session) -> StoredResume | None:
    row = session.get(ResumeRow, RESUME_ID, populate_existing=True)
    return _stored(row) if row is not None else None


def put(
    session: Session,
    *,
    filename: str,
    media_type: str,
    text: str,
    lines: Sequence[str],
    now: datetime,
) -> None:
    """Store ``text`` as the resume, replacing any other. Its vectors and anchors start empty
    and its matches need re-scoring."""
    row = session.get(ResumeRow, RESUME_ID)
    if row is None:
        row = ResumeRow(id=RESUME_ID)
        session.add(row)
    row.filename = filename
    row.media_type = media_type
    row.text = text
    row.sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    row.uploaded_at = now
    row.lines = list(lines)
    _reset_vectors(row)
    session.flush()


def _reset_vectors(row: ResumeRow) -> None:
    row.embed_model = None
    row.dims = None
    row.line_vectors = None
    row.chunk_vectors = None
    row.anchor_low = None
    row.anchor_high = None
    row.anchor_count = 0
    row.rescored_at = None


def clear(session: Session) -> bool:
    """Delete the resume; whether there was one."""
    row = session.get(ResumeRow, RESUME_ID)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def save_vectors(session: Session, *, model: str, vectors: ResumeVectors) -> None:
    """The resume's vectors from ``model``. Anchors measured with another model no longer
    apply, and its matches need re-scoring."""
    row = session.get(ResumeRow, RESUME_ID)
    if row is None:
        raise LookupError("no resume is stored")
    _reset_vectors(row)
    sample = vectors.lines[0] if vectors.lines else None
    row.embed_model = model
    row.dims = len(sample) if sample is not None else None
    row.line_vectors = encode(vectors.lines)
    row.chunk_vectors = encode(vectors.chunks)
    session.flush()


def mark_rescored(
    session: Session, *, anchors: tuple[float, float] | None, count: int, now: datetime
) -> None:
    """Open matches were re-scored at ``now``; ``anchors`` measured on ``count`` of them, or
    ``None`` for the defaults."""
    row = session.get(ResumeRow, RESUME_ID)
    if row is None:
        raise LookupError("no resume is stored")
    row.anchor_low, row.anchor_high = anchors if anchors is not None else (None, None)
    row.anchor_count = count if anchors is not None else 0
    row.rescored_at = now
    session.flush()


def posting_vectors(
    session: Session, posting_ids: Collection[int], *, model: str
) -> dict[int, tuple[str, list[Vector]]]:
    """``{posting id: (input hash, vectors)}`` for the postings embedded with ``model``."""
    found: dict[int, tuple[str, list[Vector]]] = {}
    ids = sorted(posting_ids)
    for start in range(0, len(ids), 500):
        rows = session.execute(
            select(
                PostingVector.posting_id,
                PostingVector.input_hash,
                PostingVector.dims,
                PostingVector.vectors,
            ).where(
                PostingVector.posting_id.in_(ids[start : start + 500]),
                PostingVector.model == model,
            )
        )
        for posting_id, input_hash, dims, blob in rows:
            found[posting_id] = (input_hash, decode(blob, dims=dims))
    return found


def save_posting_vectors(
    session: Session,
    items: Mapping[int, tuple[str, Sequence[Vector]]],
    *,
    model: str,
    now: datetime,
) -> None:
    """Store ``{posting id: (input hash, vectors)}`` from ``model``, replacing older ones."""
    rows = [
        {
            "posting_id": posting_id,
            "model": model,
            "input_hash": input_hash,
            "dims": len(vectors[0]),
            "vectors": encode(vectors),
            "created_at": now,
        }
        for posting_id, (input_hash, vectors) in items.items()
        if vectors
    ]
    if not rows:
        return
    ids = [row["posting_id"] for row in rows]
    for start in range(0, len(ids), 500):
        session.execute(
            delete(PostingVector).where(PostingVector.posting_id.in_(ids[start : start + 500]))
        )
    session.execute(insert(PostingVector), rows)
