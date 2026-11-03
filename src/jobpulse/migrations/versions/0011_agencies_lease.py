"""blocked_companies.kind (staffing agencies you mark) and worker_lease

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Plain types only: migrations never import application code.
UTC_DATETIME = sa.DateTime(timezone=True)


def upgrade() -> None:
    # A plain column added in place: no table rebuild (see CLAUDE.md on batch rebuilds).
    with op.batch_alter_table("blocked_companies", recreate="never") as batch_op:
        batch_op.add_column(
            sa.Column("kind", sa.String(length=10), server_default="company", nullable=False)
        )
    op.create_table(
        "worker_lease",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("holder", sa.String(length=255), nullable=False),
        sa.Column("acquired_at", UTC_DATETIME, nullable=False),
        sa.Column("expires_at", UTC_DATETIME, nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_worker_lease_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_lease")),
    )


def downgrade() -> None:
    op.drop_table("worker_lease")
    with op.batch_alter_table("blocked_companies", recreate="never") as batch_op:
        batch_op.drop_column("kind")
