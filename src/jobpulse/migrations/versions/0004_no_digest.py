"""no digest: every match is pushed

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Plain types only: migrations never import application code.
UTC_DATETIME = sa.DateTime(timezone=True)


def upgrade() -> None:
    # A posting a digest already listed counts as notified, so it isn't pushed again.
    op.execute(
        "UPDATE postings SET notified_at = digest_sent_at "
        "WHERE notified_at IS NULL AND digest_sent_at IS NOT NULL"
    )
    # Below-threshold and held-back matches become ordinary matches: pushed if still fresh.
    op.execute("UPDATE postings SET tier = 'instant' WHERE tier = 'digest'")
    # In place (SQLite 3.35+ drops an unindexed column natively). A batch rebuild would drop the
    # old table with foreign keys on, cascading into llm_assessments.
    with op.batch_alter_table("postings", recreate="never") as batch_op:
        batch_op.drop_column("digest_sent_at")


def downgrade() -> None:
    # The digest tier isn't restored: which matches were below the threshold isn't kept.
    with op.batch_alter_table("postings", recreate="never") as batch_op:
        batch_op.add_column(sa.Column("digest_sent_at", UTC_DATETIME, nullable=True))
