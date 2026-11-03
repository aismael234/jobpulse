"""early filters: employment type and repost reason

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-01
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH = 500

# A copy of jobpulse.core.employment and jobpulse.core.reposts as of this revision, for the
# backfill (migrations never import application code); a test checks they agree.
_LABELS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("contract", re.compile(r"contract|temporar|\btemp\b|fixed[\s-]*term|contingent", re.I)),
    ("internship", re.compile(r"\bintern(?:ship|s)?\b|\bintr\b|working student", re.I)),
    ("part_time", re.compile(r"part[\s_-]*time", re.I)),
    ("full_time", re.compile(r"full[\s_-]*time|\bregular\b|permanent|\bemployee\b", re.I)),
)
_PRECEDENCE = ("contract", "internship", "full_time", "part_time")
_GREENHOUSE_FIELD = re.compile(
    r"employ|time\s*type|full[\s-]*time|part[\s-]*time|regular\s+or\s+temporary|worker", re.I
)
_LINKEDIN_CRITERIA = re.compile(r"^Employment type:\s*(.+)$", re.I | re.M)
_CONTRACT_TITLE = re.compile(
    r"\bcontractor\b|\bcontract[\s-]+to[\s-]+hire\b|\btemporary\b|\btemp\b"
    r"|[(\[]\s*contract\b|\bcontract\s*[)\]]|[-–—|,:]\s*contract\s*$"
    # a leading "Contract" only before a role: "Contract Analyst" is about legal contracts
    r"|^\s*contract\s+(?=(?:senior|sr\b|junior|jr\b|lead|principal|software|back|front|full"
    r"|web|mobile|data|ml\b|machine|ai\b|devops|cloud|platform|python|java|ios|android|qa\b"
    r"|test|engineer|developer|programmer))",
    re.I,
)
_MONTHS = r"(?:\d{1,2}|three|four|six|nine|twelve|eighteen)"
_CONTRACT_DESCRIPTION = re.compile(
    rf"\b{_MONTHS}[\s-]*\+?\s*months?\s+contract\b"
    r"|\bcontract[\s-]+to[\s-]+hire\b|\bc2h\b"
    r"|\bc2c\b|\bcorp[\s-]+to[\s-]+corp\b"
    r"|\bw-?2\s+contract(?:or)?\b"
    r"|\b1099\s+(?:contract(?:ors?|s)?|basis|position|role|only)\b|\bw-?2\s*(?:or|/)\s*1099\b"
    r"|\bindependent\s+contractor\b"
    r"|\bon\s+a\s+contract(?:ual)?\s+basis\b"
    r"|\bcontract\s+(?:role|position|assignment|opportunity|engagement)\b"
    r"|\btemporary\s+(?:role|position|assignment|contract)\b",
    re.I,
)
_INTERNSHIP_TITLE = re.compile(r"\bintern(?:ship)?s?\b", re.I)
_PART_TIME_TITLE = re.compile(r"\bpart[\s-]*time\b", re.I)
REPOST_AFTER = timedelta(days=7)


def _label(label: str) -> str | None:
    return next((kind for kind, pattern in _LABELS if pattern.search(label)), None)


def _rank(labels: Iterable[str]) -> str | None:
    kinds = {kind for label in labels if (kind := _label(label)) is not None}
    return next((kind for kind in _PRECEDENCE if kind in kinds), None)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _structured(source: str, raw: Mapping[str, Any], description: str | None) -> list[str]:
    if source == "ashby":
        return _strings(raw.get("employmentType"))
    if source == "lever":
        categories = raw.get("categories")
        return _strings(categories.get("commitment")) if isinstance(categories, dict) else []
    if source == "indeed":
        return [part for value in _strings(raw.get("job_type")) for part in value.split(",")]
    if source == "workday":
        return _strings(raw.get("timeType"))
    if source == "greenhouse":
        metadata = raw.get("metadata")
        if not isinstance(metadata, list):
            return []
        return [
            label
            for item in metadata
            if isinstance(item, dict) and _GREENHOUSE_FIELD.search(str(item.get("name") or ""))
            for label in _strings(item.get("value"))
        ]
    if source == "linkedin" and description:
        return _LINKEDIN_CRITERIA.findall(description)
    return []


def employment_type(
    source: str, raw: Mapping[str, Any], title: str, description: str | None
) -> str:
    structured = _rank(_structured(source, raw, description))
    if structured is not None:
        return structured
    if _CONTRACT_TITLE.search(title):
        return "contract"
    if description and _CONTRACT_DESCRIPTION.search(description):
        return "contract"
    if _INTERNSHIP_TITLE.search(title):
        return "internship"
    if _PART_TIME_TITLE.search(title):
        return "part_time"
    return "full_time"


def requisition(source: str, raw: Mapping[str, Any]) -> str | None:
    if source != "greenhouse":
        return None
    value = raw.get("internal_job_id")
    if value is None or isinstance(value, bool) or not isinstance(value, int | str):
        return None
    return str(value).strip() or None


def _reason(first_seen_at: datetime, earliest: datetime, how: str) -> str | None:
    if first_seen_at - earliest < REPOST_AFTER:
        return None
    return f"first seen {(first_seen_at - earliest).days} days ago ({how})"


def upgrade() -> None:
    # In place: a batch rebuild would drop the old table with foreign keys on, cascading into
    # llm_assessments.
    with op.batch_alter_table("postings", recreate="never") as batch_op:
        batch_op.add_column(sa.Column("employment_type", sa.String(length=12), nullable=True))
        batch_op.add_column(sa.Column("repost_reason", sa.Text(), nullable=True))

    connection = op.get_bind()
    postings = sa.table(
        "postings",
        sa.column("id", sa.Integer()),
        sa.column("source", sa.String()),
        sa.column("company_id", sa.Integer()),
        sa.column("title", sa.Text()),
        sa.column("description_text", sa.Text()),
        sa.column("raw", sa.JSON()),
        sa.column("first_seen_at", sa.DateTime(timezone=True)),
        sa.column("tier", sa.String()),
        sa.column("status", sa.String()),
        sa.column("merged_into_id", sa.Integer()),
        sa.column("employment_type", sa.String()),
        sa.column("repost_reason", sa.Text()),
    )
    by_requisition: dict[tuple[int, str], datetime] = {}
    shown: list[tuple[int, tuple[int, str], datetime]] = []
    last_id = 0
    while True:
        rows = connection.execute(
            sa.select(
                postings.c.id,
                postings.c.source,
                postings.c.company_id,
                postings.c.title,
                postings.c.description_text,
                postings.c.raw,
                postings.c.first_seen_at,
                postings.c.tier,
                postings.c.status,
                postings.c.merged_into_id,
            )
            .where(postings.c.id > last_id)
            .order_by(postings.c.id)
            .limit(_BATCH)
        ).all()
        if not rows:
            break
        last_id = rows[-1].id
        updates = []
        for row in rows:
            raw = row.raw if isinstance(row.raw, dict) else {}
            kind = employment_type(row.source, raw, row.title, row.description_text)
            updates.append({"row_id": row.id, "kind": kind})
            req = requisition(row.source, raw)
            if req is None or not row.company_id:
                continue
            req_key, seen = (row.company_id, req), row.first_seen_at
            if seen < by_requisition.get(req_key, seen + REPOST_AFTER):
                by_requisition[req_key] = seen
            # Only rows the dashboard can show: matches, and anything triaged.
            if row.merged_into_id is None and (row.tier == "instant" or row.status != "New"):
                shown.append((row.id, req_key, seen))
        connection.execute(
            postings.update()
            .where(postings.c.id == sa.bindparam("row_id"))
            .values(employment_type=sa.bindparam("kind")),
            updates,
        )

    reposts = []
    for row_id, req_key, seen in shown:
        reason = _reason(seen, by_requisition[req_key], f"requisition {req_key[1]}")
        if reason is not None:
            reposts.append({"row_id": row_id, "reason": reason})
    for start in range(0, len(reposts), _BATCH):
        connection.execute(
            postings.update()
            .where(postings.c.id == sa.bindparam("row_id"))
            .values(repost_reason=sa.bindparam("reason")),
            reposts[start : start + _BATCH],
        )


def downgrade() -> None:
    with op.batch_alter_table("postings", recreate="never") as batch_op:
        batch_op.drop_column("repost_reason")
        batch_op.drop_column("employment_type")
