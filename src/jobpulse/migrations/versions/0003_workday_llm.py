"""workday keywords, llm assessments

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Plain types only: migrations never import application code.
UTC_DATETIME = sa.DateTime(timezone=True)
JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "llm_assessments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("posting_id", sa.Integer(), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.Column("ok", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("level", sa.String(length=20), nullable=True),
        sa.Column("backend_focus", sa.Double(), nullable=True),
        sa.Column("fit", sa.Double(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["posting_id"],
            ["postings.id"],
            name=op.f("fk_llm_assessments_posting_id_postings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_assessments")),
    )
    op.create_index(
        op.f("ix_llm_assessments_created_at"), "llm_assessments", ["created_at"], unique=False
    )
    op.create_index(
        "ix_llm_assessments_posting_id_input_hash",
        "llm_assessments",
        ["posting_id", "input_hash"],
        unique=False,
    )
    with op.batch_alter_table("companies") as batch_op:
        batch_op.add_column(sa.Column("params", JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("companies") as batch_op:
        batch_op.drop_column("params")
    op.drop_index("ix_llm_assessments_posting_id_input_hash", table_name="llm_assessments")
    op.drop_index(op.f("ix_llm_assessments_created_at"), table_name="llm_assessments")
    op.drop_table("llm_assessments")
