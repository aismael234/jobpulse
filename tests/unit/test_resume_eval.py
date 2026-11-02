import csv
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from jobpulse.core.filters import Blocklist, FilterRules
from jobpulse.db.session import create_engine_for, make_session_factory
from jobpulse.db.tables import Base, PostingRow
from jobpulse.devtools.resume_eval import (
    VARIANTS,
    auc,
    load_labeled,
    load_reference,
    render,
    run_eval,
    write_csv,
)
from tests.factories import NOW, FakeEmbedder, compiled, posting_row

RESUME = """Jane Doe
jane@example.com | 555-123-4567
● Built Python APIs on PostgreSQL for payments backend services.
● Wrote Python data pipelines and REST APIs with PostgreSQL and Redis.
"""
FIT = "We build Python APIs on PostgreSQL for payments. Backend services, REST, Redis."
UNFIT = "Sell enterprise marketing software to retail chains. Quota, travel, cold calls."
BREAKDOWN = {"freshness": 0.2, "title": 0.3, "stack": 0.1, "level_mismatch": 0.0, "total": 0.6}


@pytest.fixture
def session(tmp_path: Path) -> Session:
    engine = create_engine_for(f"sqlite:///{tmp_path / 'eval.db'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        yield session


def add(session: Session, **values: Any) -> PostingRow:
    values.setdefault("score_breakdown", dict(BREAKDOWN))
    row = posting_row(**values)
    session.add(row)
    session.flush()
    return row


def seed(session: Session) -> None:
    for i in range(4):
        add(
            session,
            title=f"Backend Engineer {'ABCD'[i]}",
            status="Applied",
            status_changed_at=NOW - timedelta(days=i + 1),
            description_text=FIT,
        )
    add(session, title="Applied Without Text", status="Applied", description_text=None)
    for i in range(4):
        add(
            session,
            title=f"Account Executive {'ABCD'[i]}",
            status="Dismissed",
            status_changed_at=NOW - timedelta(days=i + 1, minutes=i),
            description_text=UNFIT,
        )
    add(
        session,
        title="Backend Engineer Blocked",
        company_name="Blockedco",
        status="Dismissed",
        status_changed_at=NOW - timedelta(days=2),
        description_text=FIT,
    )
    add(
        session,
        title="Senior Staff Backend Engineer",
        status="Dismissed",
        status_changed_at=NOW - timedelta(days=2, hours=1),
        description_text=FIT,
    )
    bulk_time = NOW - timedelta(days=3)
    for i in range(2):
        add(
            session,
            title=f"Bulk Engineer {'AB'[i]}",
            status="Dismissed",
            status_changed_at=bulk_time,
            description_text=FIT,
        )
    add(
        session,
        title="Old Triage Engineer",
        status="Dismissed",
        status_changed_at=None,
        description_text=FIT,
    )
    for i in range(6):
        add(
            session,
            title=f"Recent Match {'ABCDEF'[i]}",
            description_text=FIT if i % 2 else UNFIT,
            first_seen_at=NOW - timedelta(hours=i),
        )
    add(
        session,
        title="Old Match",
        description_text=FIT,
        first_seen_at=NOW - timedelta(days=30),
    )
    session.commit()


RULES = FilterRules(
    title_exclude=compiled(r"\bsenior\b"),
    blocklist=Blocklist.build(companies=["Blockedco"]),
)


def test_auc_counts_ties_as_half() -> None:
    assert auc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert auc([0.1], [0.9]) == 0.0
    assert auc([0.5], [0.5]) == 0.5
    assert auc([], [0.5]) is None


def test_dismissals_with_other_causes_are_set_aside(session: Session) -> None:
    seed(session)
    labeled = load_labeled(session, RULES)
    groups = {row.title: row.group for row in labeled}
    assert groups["Backend Engineer A"] == "applied"
    assert groups["Account Executive A"] == "kept"
    assert groups["Backend Engineer Blocked"] == "rules"
    assert groups["Senior Staff Backend Engineer"] == "rules"
    assert groups["Bulk Engineer A"] == groups["Bulk Engineer B"] == "bulk"
    assert groups["Old Triage Engineer"] == "pre-0005"
    assert "Applied Without Text" not in groups  # only rows with a description are scored
    blocked = next(row for row in labeled if row.title == "Backend Engineer Blocked")
    assert "blocklist" in (blocked.set_aside or "")


def test_reference_is_recent_matches_with_a_description(session: Session) -> None:
    seed(session)
    titles = {row.title for row in load_reference(session, days=7)}
    assert {f"Recent Match {c}" for c in "ABCDEF"} <= titles
    assert "Old Match" not in titles


def test_run_eval_separates_the_classes_and_reports_each_group(
    session: Session, tmp_path: Path
) -> None:
    seed(session)
    embedder = FakeEmbedder()
    report = run_eval(
        load_labeled(session, RULES),
        load_reference(session, days=7),
        resume_text=RESUME,
        embedder=embedder,
        bootstrap=50,
    )
    assert len(report.variants) == len(VARIANTS)
    assert report.best.auc == 1.0
    desc_only = [r for r in report.variants if r.variant.text == "desc"]
    assert all(r.by_group["bulk"] == 0.5 for r in desc_only)  # the applied ones' own text
    assert report.counts["applied"] == 4
    assert report.counts["kept"] == 4
    low, high = report.best.anchors
    assert low <= high
    assert [weight for weight, *_ in report.simulated] == [0.1, 0.15, 0.2, 0.25, 0.3, 0.4]
    assert report.embedded_texts == len(set(embedder.texts))  # each text embedded once

    text = render(report)
    assert "set aside" in text
    assert "stored total" in text
    for private in ("jane@example.com", "555-123-4567", "Jane Doe"):
        assert private not in text
        assert all(private not in t for t in embedder.texts)

    path = tmp_path / "eval.csv"
    write_csv(report, path)
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == len(report.rows)
    assert {"id", "status", "group", "title", "stack", "total"} <= set(rows[0])
    assert VARIANTS[0].name in rows[0]
