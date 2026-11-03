"""saved filters: saved_filters, filter_pushes, preferences, postings.decided_at

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Plain types only: migrations never import application code.
UTC_DATETIME = sa.DateTime(timezone=True)
JSON = sa.JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    # In place: a batch rebuild would drop the old table with foreign keys on, cascading into
    # llm_assessments.
    with op.batch_alter_table("postings", recreate="never") as batch_op:
        batch_op.add_column(sa.Column("decided_at", UTC_DATETIME, nullable=True))
    op.create_table(
        "saved_filters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("params", JSON, nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("fires_from", UTC_DATETIME, nullable=False),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.Column("last_viewed_at", UTC_DATETIME, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_filters")),
        sa.UniqueConstraint("slot", name=op.f("uq_saved_filters_slot")),
    )
    op.create_table(
        "filter_pushes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("posting_id", sa.Integer(), nullable=False),
        sa.Column("filter_id", sa.Integer(), nullable=False),
        sa.Column("sent_at", UTC_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(
            ["filter_id"],
            ["saved_filters.id"],
            name=op.f("fk_filter_pushes_filter_id_saved_filters"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["posting_id"],
            ["postings.id"],
            name=op.f("fk_filter_pushes_posting_id_postings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_filter_pushes")),
        sa.UniqueConstraint(
            "posting_id", "filter_id", name=op.f("uq_filter_pushes_posting_id_filter_id")
        ),
    )
    op.create_index(op.f("ix_filter_pushes_sent_at"), "filter_pushes", ["sent_at"], unique=False)
    op.create_table(
        "preferences",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("value", JSON, nullable=False),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_preferences")),
    )


def downgrade() -> None:
    op.drop_table("preferences")
    op.drop_index(op.f("ix_filter_pushes_sent_at"), table_name="filter_pushes")
    op.drop_table("filter_pushes")
    op.drop_table("saved_filters")
    with op.batch_alter_table("postings", recreate="never") as batch_op:
        batch_op.drop_column("decided_at")
