"""feed state, health incidents, discovery provenance

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Plain types only: migrations never import application code.
UTC_DATETIME = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "source_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("baseline_done_at", UTC_DATETIME, nullable=True),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_state")),
        sa.UniqueConstraint("source", "target", name=op.f("uq_source_state_source_target")),
    )
    op.create_table(
        "health_incidents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("opened_at", UTC_DATETIME, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("alerted_at", UTC_DATETIME, nullable=True),
        sa.Column("resolved_at", UTC_DATETIME, nullable=True),
        sa.Column("recovery_sent_at", UTC_DATETIME, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_health_incidents")),
    )
    op.create_index(
        "ix_health_incidents_source_target_kind",
        "health_incidents",
        ["source", "target", "kind"],
    )
    with op.batch_alter_table("companies") as batch_op:
        batch_op.add_column(sa.Column("discovered_from", sa.Text(), nullable=True))
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(
            sa.Column("not_modified", sa.Boolean(), server_default=sa.false(), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("not_modified")
    with op.batch_alter_table("companies") as batch_op:
        batch_op.drop_column("discovered_from")
    op.drop_index("ix_health_incidents_source_target_kind", table_name="health_incidents")
    op.drop_table("health_incidents")
    op.drop_table("source_state")
