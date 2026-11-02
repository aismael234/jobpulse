"""Re-scoring open matches after the profile's weights or rules change (M6).

Each open, untriaged match (New or Saved) is scored again as ``evaluate`` would have scored it
when it was decided, with today's weights, title and stack rules, company priority and caps.
Its resume part is the stored resume score, and the LLM's part is kept as stored. Tiers never
change here: a stricter rule doesn't hide an open match, it only scores it lower.
"""

import math
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from jobpulse.core.detection import EvaluationContext, match_score
from jobpulse.core.filters import FilterRules
from jobpulse.core.models import ATS_SOURCES
from jobpulse.core.scoring import DREAM_PRIORITY, Weights, priority_component
from jobpulse.core.similarity import ResumeMatch
from jobpulse.db import repo
from jobpulse.db.tables import Company, PostingRow
from jobpulse.resume_scoring import in_scope


def rescore_matches(
    session: Session,
    *,
    rules: FilterRules,
    weights: Weights,
    freshness: timedelta,
    priority_cap: int,
    applicant_cap: int,
) -> int:
    """Re-score every open match; returns how many scores changed."""
    rows = session.execute(
        select(PostingRow, Company.priority)
        .outerjoin(Company, Company.id == PostingRow.company_id)
        .where(*in_scope(), PostingRow.score_breakdown.is_not(None))
    ).all()
    changes: list[dict[str, object]] = []
    for row, priority in rows:
        company = row.source in ATS_SOURCES and priority is not None
        context = EvaluationContext(
            now=row.decided_at or row.first_seen_at,
            first_seen_at=row.first_seen_at,
            freshness=freshness,
            rules=rules,
            weights=weights,
            company_priority=(
                DREAM_PRIORITY
                if rules.is_dream(row.company_name)
                else priority_component(priority, priority_cap)
                if company
                else 0.0
            ),
            applicant_cap=applicant_cap,
            resume=(
                ResumeMatch(row.resume_score, row.resume_line or "")
                if row.resume_score is not None
                else None
            ),
        )
        total, breakdown = match_score(repo.row_to_posting(row), context)
        llm = (row.score_breakdown or {}).get("llm")
        if llm is not None:
            total = round(math.fsum((total, llm)), 4) + 0.0
            breakdown = {**breakdown, "llm": llm, "total": total}
        if total != row.score or breakdown != row.score_breakdown:
            changes.append({"id": row.id, "score": total, "score_breakdown": breakdown})
    if changes:
        session.execute(update(PostingRow), changes)
    return len(changes)
