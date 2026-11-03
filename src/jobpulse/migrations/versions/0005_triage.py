"""triage: years asked, status times, dashboard blocklist

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-27
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Plain types only: migrations never import application code.
UTC_DATETIME = sa.DateTime(timezone=True)

# A copy of jobpulse.core.filters.extract_years_required as of this revision, for the backfill;
# a test checks the two agree.
_YEARS = re.compile(
    r"(?P<min_phrase>\b(?:minimum(?:\s+of)?|at\s+least|min\.?)\s+)?"
    r"(?P<low>\d{1,2})\s*(?P<plus>\+)?\s*"
    r"(?:(?:-|–|—|to)\s*(?P<high>\d{1,2})\s*\+?\s*)?"
    r"(?:years?|yrs?)\b",
    re.IGNORECASE,
)
_EXPERIENCE = re.compile(r"\bexperience\b", re.IGNORECASE)
_MAX_PLAUSIBLE_YEARS = 30
_CONTEXT_BEFORE = 60
_CONTEXT_AFTER = 80
_BATCH = 500


def years_required(text: str | None) -> int | None:
    if not text:
        return None
    found: list[int] = []
    for match in _YEARS.finditer(text):
        low = int(match["low"])
        if low > _MAX_PLAUSIBLE_YEARS:
            continue
        if not (match["plus"] or match["high"] or match["min_phrase"]):
            window = text[max(0, match.start() - _CONTEXT_BEFORE) : match.end() + _CONTEXT_AFTER]
            if not _EXPERIENCE.search(window):
                continue
        found.append(low)
    return max(found) if found else None


def upgrade() -> None:
    # In place: a batch rebuild would drop the old table with foreign keys on, cascading into
    # llm_assessments.
    with op.batch_alter_table("postings", recreate="never") as batch_op:
        batch_op.add_column(sa.Column("status_changed_at", UTC_DATETIME, nullable=True))
        batch_op.add_column(sa.Column("years_required", sa.Integer(), nullable=True))
    op.create_table(
        "blocked_companies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_key", sa.String(length=255), nullable=False),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blocked_companies")),
        sa.UniqueConstraint("name_key", name=op.f("uq_blocked_companies_name_key")),
    )

    connection = op.get_bind()
    postings = sa.table(
        "postings",
        sa.column("id", sa.Integer()),
        sa.column("description_text", sa.Text()),
        sa.column("years_required", sa.Integer()),
    )
    last_id = 0
    while True:
        rows = connection.execute(
            sa.select(postings.c.id, postings.c.description_text)
            .where(postings.c.id > last_id, postings.c.description_text.is_not(None))
            .order_by(postings.c.id)
            .limit(_BATCH)
        ).all()
        if not rows:
            break
        last_id = rows[-1].id
        updates = [
            {"row_id": row.id, "years": years}
            for row in rows
            if (years := years_required(row.description_text)) is not None
        ]
        if updates:
            connection.execute(
                postings.update()
                .where(postings.c.id == sa.bindparam("row_id"))
                .values(years_required=sa.bindparam("years")),
                updates,
            )


def downgrade() -> None:
    op.drop_table("blocked_companies")
    with op.batch_alter_table("postings", recreate="never") as batch_op:
        batch_op.drop_column("years_required")
        batch_op.drop_column("status_changed_at")
