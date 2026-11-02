"""profile and secrets: preferences and Settings secrets in the database

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Plain types only: migrations never import application code.
UTC_DATETIME = sa.DateTime(timezone=True)
JSON = sa.JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    # New tables only: config.yaml's preferences come in through `jobpulse profile import`.
    op.create_table(
        "profile",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("data", JSON, nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("onboarded_at", UTC_DATETIME, nullable=True),
        sa.Column("scored_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_profile_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profile")),
    )
    op.create_table(
        "secrets",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_secrets")),
    )


def downgrade() -> None:
    op.drop_table("secrets")
    op.drop_table("profile")
