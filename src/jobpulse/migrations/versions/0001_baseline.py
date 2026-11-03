"""baseline: companies, searches, postings, runs

Revision ID: 0001
Revises:
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Plain types only: migrations never import application code.
UTC_DATETIME = sa.DateTime(timezone=True)
JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("ats_type", sa.String(length=20), nullable=False),
        sa.Column("token", sa.String(length=255), nullable=False),
        sa.Column("region", sa.String(length=8), server_default="us", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "source_of_discovery", sa.String(length=20), server_default="config", nullable=False
        ),
        sa.Column("baseline_done_at", UTC_DATETIME, nullable=True),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companies")),
        sa.UniqueConstraint("ats_type", "token", name=op.f("uq_companies_ats_type_token")),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=False),
        sa.Column("started_at", UTC_DATETIME, nullable=False),
        sa.Column("finished_at", UTC_DATETIME, nullable=True),
        sa.Column("ok", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("fetched_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("new_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("error_kind", sa.String(length=32), nullable=True),
        sa.Column("warnings", JSON, nullable=True),
        sa.Column("stats", JSON, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    op.create_index("ix_runs_source_target_started_at", "runs", ["source", "target", "started_at"])
    op.create_table(
        "searches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("site", sa.String(length=20), nullable=False),
        sa.Column("search_term", sa.Text(), nullable=False),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("hours_old", sa.Integer(), server_default="3", nullable=False),
        sa.Column("params", JSON, nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("baseline_done_at", UTC_DATETIME, nullable=True),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_searches")),
        sa.UniqueConstraint("name", name=op.f("uq_searches_name")),
    )
    op.create_table(
        "postings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("company_name", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("locations", JSON, nullable=False),
        sa.Column("is_remote", sa.Boolean(), nullable=True),
        sa.Column("workplace_type", sa.String(length=20), nullable=True),
        sa.Column("department", sa.Text(), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("apply_url", sa.Text(), nullable=True),
        sa.Column("description_text", sa.Text(), nullable=True),
        sa.Column("compensation", JSON, nullable=True),
        sa.Column("posted_at", UTC_DATETIME, nullable=True),
        sa.Column(
            "posted_at_precision", sa.String(length=8), server_default="none", nullable=False
        ),
        sa.Column("first_seen_at", UTC_DATETIME, nullable=False),
        sa.Column("last_seen_at", UTC_DATETIME, nullable=False),
        sa.Column("closed_at", UTC_DATETIME, nullable=True),
        sa.Column("missing_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tier", sa.String(length=12), server_default="pending", nullable=False),
        sa.Column("tier_reason", sa.Text(), nullable=True),
        sa.Column("score", sa.Double(), nullable=True),
        sa.Column("score_breakdown", JSON, nullable=True),
        sa.Column("applicant_label", sa.String(length=100), nullable=True),
        sa.Column("applicant_min", sa.Integer(), nullable=True),
        sa.Column("applicant_max", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=12), server_default="New", nullable=False),
        sa.Column("notified_at", UTC_DATETIME, nullable=True),
        sa.Column("digest_sent_at", UTC_DATETIME, nullable=True),
        sa.Column("merged_into_id", sa.Integer(), nullable=True),
        sa.Column("also_seen_on", JSON, nullable=False),
        sa.Column("raw", JSON, nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_postings_company_id_companies"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["merged_into_id"],
            ["postings.id"],
            name=op.f("fk_postings_merged_into_id_postings"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_postings")),
        sa.UniqueConstraint("source", "external_id", name=op.f("uq_postings_source_external_id")),
    )
    op.create_index(op.f("ix_postings_canonical_url"), "postings", ["canonical_url"])
    op.create_index(op.f("ix_postings_fingerprint"), "postings", ["fingerprint"])
    op.create_index(op.f("ix_postings_first_seen_at"), "postings", ["first_seen_at"])
    op.create_index("ix_postings_company_id_closed_at", "postings", ["company_id", "closed_at"])
    op.create_index("ix_postings_tier_notified_at", "postings", ["tier", "notified_at"])


def downgrade() -> None:
    op.drop_index("ix_postings_tier_notified_at", table_name="postings")
    op.drop_index("ix_postings_company_id_closed_at", table_name="postings")
    op.drop_index(op.f("ix_postings_first_seen_at"), table_name="postings")
    op.drop_index(op.f("ix_postings_fingerprint"), table_name="postings")
    op.drop_index(op.f("ix_postings_canonical_url"), table_name="postings")
    op.drop_table("postings")
    op.drop_table("searches")
    op.drop_index("ix_runs_source_target_started_at", table_name="runs")
    op.drop_table("runs")
    op.drop_table("companies")
