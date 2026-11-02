"""ORM models (SQLAlchemy 2.x, typed). All timestamps are timezone-aware UTC."""

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from jobpulse.core.timeparse import utcnow
from jobpulse.db.types import JSONType, UTCDateTime

# Stable constraint names on every backend; SQLite batch migrations need them.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: UTCDateTime()}


class Company(Base):
    """A watchlist company on one ATS board."""

    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("ats_type", "token"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    ats_type: Mapped[str] = mapped_column(String(20))
    token: Mapped[str] = mapped_column(String(255))
    region: Mapped[str] = mapped_column(String(8), default="us", server_default="us")
    priority: Mapped[int] = mapped_column(default=0, server_default="0")
    active: Mapped[bool] = mapped_column(default=True, server_default=true())
    source_of_discovery: Mapped[str] = mapped_column(
        String(20), default="config", server_default="config"
    )
    discovered_from: Mapped[str | None] = mapped_column(Text)  # the URL that led to an auto add
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONType)  # Workday: {"keywords": [...]}
    baseline_done_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Search(Base):
    """A job-board search (Indeed or LinkedIn). Its first successful run is a silent baseline."""

    __tablename__ = "searches"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    site: Mapped[str] = mapped_column(String(20))
    search_term: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    hours_old: Mapped[int] = mapped_column(default=3, server_default="3")
    params: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    active: Mapped[bool] = mapped_column(default=True, server_default=true())
    baseline_done_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PostingRow(Base):
    """One posting, stored under its stable (source, external_id) key."""

    __tablename__ = "postings"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        Index("ix_postings_company_id_closed_at", "company_id", "closed_at"),
        Index("ix_postings_tier_notified_at", "tier", "notified_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(255))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))
    company_name: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    locations: Mapped[list[str]] = mapped_column(JSONType, default=list)
    is_remote: Mapped[bool | None]
    workplace_type: Mapped[str | None] = mapped_column(String(20))
    department: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    url: Mapped[str] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(Text)
    description_text: Mapped[str | None] = mapped_column(Text)
    compensation: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    posted_at: Mapped[datetime | None]
    posted_at_precision: Mapped[str] = mapped_column(
        String(8), default="none", server_default="none"
    )
    first_seen_at: Mapped[datetime] = mapped_column(index=True)
    last_seen_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]
    missing_count: Mapped[int] = mapped_column(default=0, server_default="0")
    tier: Mapped[str] = mapped_column(String(12), default="pending", server_default="pending")
    tier_reason: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None]
    score_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    applicant_label: Mapped[str | None] = mapped_column(String(100))
    applicant_min: Mapped[int | None]
    applicant_max: Mapped[int | None]
    status: Mapped[str] = mapped_column(String(12), default="New", server_default="New")
    status_changed_at: Mapped[datetime | None]  # the last move between dashboard views
    years_required: Mapped[int | None]  # the most years of experience the description asks
    employment_type: Mapped[str | None] = mapped_column(String(12))  # core.employment
    repost_reason: Mapped[str | None] = mapped_column(Text)  # NULL: not a repost (core.reposts)
    decided_at: Mapped[datetime | None]  # when the cycle decided it (saved filters fire after)
    resume_score: Mapped[float | None]  # calibrated resume similarity (M5b), when scored
    resume_line: Mapped[str | None] = mapped_column(Text)  # the closest resume line
    notified_at: Mapped[datetime | None]
    merged_into_id: Mapped[int | None] = mapped_column(
        ForeignKey("postings.id", ondelete="SET NULL")
    )
    also_seen_on: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class BlockedCompany(Base):
    """A company blocked from the dashboard. config.yaml's ``blocklist.companies`` and these
    rows together make the blocklist."""

    __tablename__ = "blocked_companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)  # as the posting showed it
    name_key: Mapped[str] = mapped_column(String(255), unique=True)  # normalize_company(name)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Run(Base):
    """One fetch of one target (a company board, a search or a feed)."""

    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_source_target_started_at", "source", "target", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20))
    target: Mapped[str] = mapped_column(String(255))
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    ok: Mapped[bool] = mapped_column(default=False, server_default=false())
    fetched_count: Mapped[int] = mapped_column(default=0, server_default="0")
    new_count: Mapped[int] = mapped_column(default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    error_kind: Mapped[str | None] = mapped_column(String(32))
    not_modified: Mapped[bool] = mapped_column(default=False, server_default=false())  # a 304
    warnings: Mapped[list[str] | None] = mapped_column(JSONType)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONType)


class SourceState(Base):
    """What a feed needs between polls: the ETag to send next and its silent baseline."""

    __tablename__ = "source_state"
    __table_args__ = (UniqueConstraint("source", "target"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20))
    target: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    etag: Mapped[str | None] = mapped_column(Text)
    baseline_done_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class HealthIncident(Base):
    """One health problem of one target: opened, alerted once, resolved, recovery sent once."""

    __tablename__ = "health_incidents"
    __table_args__ = (Index("ix_health_incidents_source_target_kind", "source", "target", "kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20))
    target: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16))  # failing | silent
    opened_at: Mapped[datetime]  # when the problem began, not when it was noticed
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text)
    alerted_at: Mapped[datetime | None]
    resolved_at: Mapped[datetime | None]
    recovery_sent_at: Mapped[datetime | None]


class LlmAssessment(Base):
    """One LLM call about one posting: its answer (the per-posting cache) or its error. Every
    row counts toward ``llm.max_calls_per_day``."""

    __tablename__ = "llm_assessments"
    __table_args__ = (
        Index("ix_llm_assessments_posting_id_input_hash", "posting_id", "input_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    posting_id: Mapped[int] = mapped_column(ForeignKey("postings.id", ondelete="CASCADE"))
    input_hash: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(index=True)
    ok: Mapped[bool] = mapped_column(default=False, server_default=false())
    level: Mapped[str | None] = mapped_column(String(20))
    backend_focus: Mapped[float | None]
    fit: Mapped[float | None]
    reason: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(default=0, server_default="0")


class SavedFilter(Base):
    """A labeled set of dashboard filters (M5.1). While any exist, only new matches that fit an
    active one are pushed, one message each. ``params`` holds every filter written out, read
    through ``db.query_params``; ``fires_from`` moves on save, edit and switch-on, so a filter
    never fires for postings decided before it."""

    __tablename__ = "saved_filters"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(Text)
    slot: Mapped[int] = mapped_column(unique=True)  # its color, core.palette
    params: Mapped[dict[str, Any]] = mapped_column(JSONType)
    active: Mapped[bool] = mapped_column(default=True, server_default=true())
    position: Mapped[int] = mapped_column(default=0, server_default="0")
    fires_from: Mapped[datetime]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    last_viewed_at: Mapped[datetime | None]  # the dashboard badge counts matches since


class FilterPush(Base):
    """A posting pushed on its own for one saved filter it fit (the daily cap counts these)."""

    __tablename__ = "filter_pushes"
    __table_args__ = (UniqueConstraint("posting_id", "filter_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    posting_id: Mapped[int] = mapped_column(ForeignKey("postings.id", ondelete="CASCADE"))
    filter_id: Mapped[int] = mapped_column(ForeignKey("saved_filters.id", ondelete="CASCADE"))
    sent_at: Mapped[datetime] = mapped_column(index=True)


class Preference(Base):
    """A dashboard preference kept in the database (``color_blind``)."""

    __tablename__ = "preferences"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONType)
    updated_at: Mapped[datetime]


class ResumeRow(Base):
    """The one resume postings are compared with (M5b). ``lines`` are its content lines
    (``core.similarity.resume_lines``: no headings or contact details); the vectors are from
    ``embed_model`` and are dropped whenever the text changes. ``rescored_at`` NULL means open
    matches still need re-scoring against it."""

    __tablename__ = "resume"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    filename: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    uploaded_at: Mapped[datetime]
    lines: Mapped[list[str]] = mapped_column(JSONType)
    embed_model: Mapped[str | None] = mapped_column(String(100))
    dims: Mapped[int | None]
    line_vectors: Mapped[bytes | None] = mapped_column(LargeBinary)
    chunk_vectors: Mapped[bytes | None] = mapped_column(LargeBinary)
    anchor_low: Mapped[float | None]  # NULL: core.similarity.DEFAULT_ANCHORS
    anchor_high: Mapped[float | None]
    anchor_count: Mapped[int] = mapped_column(default=0, server_default="0")
    rescored_at: Mapped[datetime | None]


class PostingVector(Base):
    """A posting's description embedded (M5b), cached by model and input hash."""

    __tablename__ = "posting_vectors"

    posting_id: Mapped[int] = mapped_column(
        ForeignKey("postings.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String(100))
    input_hash: Mapped[str] = mapped_column(String(64))
    dims: Mapped[int]
    vectors: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime]


class ProfileRow(Base):
    """Your preferences (``config.Profile``) as JSON, one row (M6). ``revision`` goes up with
    every save, so the worker and the dashboard know when to recompile; ``onboarded_at`` NULL
    means the worker waits. ``scored_hash`` names the filters and scoring open matches were
    last scored with (``config.scoring_hash``)."""

    __tablename__ = "profile"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType)
    revision: Mapped[int]
    onboarded_at: Mapped[datetime | None]
    scored_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class SecretRow(Base):
    """A secret saved on Settings: an ntfy topic, a Discord webhook, an API key. The same
    secret in ``.env`` wins. Plain text, like ``.env``: keep the database and its backups
    private."""

    __tablename__ = "secrets"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime]


class SeedState(Base):
    """The public seed's last sync (M7), one row: the ETag to send next, saved in the same
    commit as the boards that sync added."""

    __tablename__ = "seed_state"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    url: Mapped[str] = mapped_column(Text)
    etag: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime]  # the last fetch, 304s included
    synced_at: Mapped[datetime | None]  # the last fetch that brought a body and applied it
