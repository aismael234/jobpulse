"""seed_state: the public seed's last sync

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Plain types only: migrations never import application code.
UTC_DATETIME = sa.DateTime(timezone=True)


def upgrade() -> None:
    # A new table only; seed boards are companies rows (source_of_discovery "seed").
    op.create_table(
        "seed_state",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("checked_at", UTC_DATETIME, nullable=False),
        sa.Column("synced_at", UTC_DATETIME, nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_seed_state_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seed_state")),
    )


def downgrade() -> None:
    op.drop_table("seed_state")
