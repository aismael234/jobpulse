"""resume vectors: resume, posting_vectors, postings.resume_score and resume_line

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Plain types only: migrations never import application code.
UTC_DATETIME = sa.DateTime(timezone=True)
JSON = sa.JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    # In place: a batch rebuild would drop the old table with foreign keys on, cascading into
    # llm_assessments and filter_pushes.
    with op.batch_alter_table("postings", recreate="never") as batch_op:
        batch_op.add_column(sa.Column("resume_score", sa.Double(), nullable=True))
        batch_op.add_column(sa.Column("resume_line", sa.Text(), nullable=True))
    op.create_table(
        "resume",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(length=100), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("uploaded_at", UTC_DATETIME, nullable=False),
        sa.Column("lines", JSON, nullable=False),
        sa.Column("embed_model", sa.String(length=100), nullable=True),
        sa.Column("dims", sa.Integer(), nullable=True),
        sa.Column("line_vectors", sa.LargeBinary(), nullable=True),
        sa.Column("chunk_vectors", sa.LargeBinary(), nullable=True),
        sa.Column("anchor_low", sa.Double(), nullable=True),
        sa.Column("anchor_high", sa.Double(), nullable=True),
        sa.Column("anchor_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rescored_at", UTC_DATETIME, nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_resume_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resume")),
    )
    op.create_table(
        "posting_vectors",
        sa.Column("posting_id", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("dims", sa.Integer(), nullable=False),
        sa.Column("vectors", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(
            ["posting_id"],
            ["postings.id"],
            name=op.f("fk_posting_vectors_posting_id_postings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("posting_id", name=op.f("pk_posting_vectors")),
    )


def downgrade() -> None:
    op.drop_table("posting_vectors")
    op.drop_table("resume")
    with op.batch_alter_table("postings", recreate="never") as batch_op:
        batch_op.drop_column("resume_line")
        batch_op.drop_column("resume_score")
