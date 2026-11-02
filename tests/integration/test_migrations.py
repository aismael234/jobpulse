import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from jobpulse.cli import app
from jobpulse.core.employment import employment_type
from jobpulse.core.filters import extract_years_required
from jobpulse.core.reposts import requisition
from jobpulse.db.tables import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
TABLES = {"companies", "searches", "postings", "runs", "source_state", "health_incidents"}


def alembic_config(url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'migrate.db'}"


def test_upgrade_head_creates_the_schema(db_url: str) -> None:
    command.upgrade(alembic_config(db_url), "head")
    inspector = inspect(create_engine(db_url))

    assert TABLES | {"alembic_version"} <= set(inspector.get_table_names())
    posting_uniques = {
        tuple(u["column_names"]) for u in inspector.get_unique_constraints("postings")
    }
    assert ("source", "external_id") in posting_uniques
    company_uniques = {
        tuple(u["column_names"]) for u in inspector.get_unique_constraints("companies")
    }
    assert ("ats_type", "token") in company_uniques
    posting_indexes = {tuple(i["column_names"]) for i in inspector.get_indexes("postings")}
    assert {("canonical_url",), ("fingerprint",), ("first_seen_at",)} <= posting_indexes


def test_migrations_match_the_models(db_url: str) -> None:
    command.upgrade(alembic_config(db_url), "head")
    with create_engine(db_url).connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


def test_downgrade_to_base_drops_every_table(db_url: str) -> None:
    config = alembic_config(db_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    assert set(inspect(create_engine(db_url)).get_table_names()) <= {"alembic_version"}


def test_0004_turns_digest_rows_into_matches_without_pushing_digested_ones(db_url: str) -> None:
    config = alembic_config(db_url)
    command.upgrade(config, "0003")
    engine = create_engine(db_url)
    insert = text(
        "INSERT INTO postings (id, source, external_id, company_name, title, locations, "
        "canonical_url, fingerprint, url, first_seen_at, last_seen_at, also_seen_on, raw, "
        "tier, notified_at, digest_sent_at) VALUES (:id, 'greenhouse', :id, 'Acme', 'Role', "
        "'[]', :id, 'f', 'https://example.com', :seen, :seen, '[]', '{}', :tier, :notified, "
        ":digested)"
    )
    seen, sent = "2026-10-24 08:00:00.000000", "2026-10-24 09:00:00.000000"
    with engine.begin() as connection:
        for row in (
            {"id": 1, "tier": "digest", "notified": None, "digested": None},
            {"id": 2, "tier": "digest", "notified": None, "digested": sent},
            {"id": 3, "tier": "instant", "notified": seen, "digested": None},
            {"id": 4, "tier": "filtered", "notified": None, "digested": None},
        ):
            connection.execute(insert, {"seen": seen, **row})
        connection.execute(
            text(
                "INSERT INTO llm_assessments (posting_id, input_hash, model, created_at) "
                "VALUES (1, 'h', 'm', :seen)"
            ),
            {"seen": seen},
        )
        connection.execute(text("UPDATE postings SET merged_into_id = 1 WHERE id = 4"))

    command.upgrade(config, "0004")

    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, tier, notified_at FROM postings ORDER BY id")
        ).all()
    assert [tuple(row) for row in rows] == [
        (1, "instant", None),  # pushed by the next cycle while it's fresh
        (2, "instant", sent),  # a digest listed it: not pushed again
        (3, "instant", seen),
        (4, "filtered", None),
    ]
    assert "digest_sent_at" not in {c["name"] for c in inspect(engine).get_columns("postings")}
    with engine.connect() as connection:  # the column is dropped in place: no foreign-key actions
        assert connection.execute(text("SELECT count(*) FROM llm_assessments")).scalar() == 1
        merged = text("SELECT merged_into_id FROM postings WHERE id = 4")
        assert connection.execute(merged).scalar() == 1

    command.downgrade(config, "0003")
    assert "digest_sent_at" in {c["name"] for c in inspect(engine).get_columns("postings")}


YEARS_SAMPLES = (
    None,
    "",
    "We have been in business for 10 years.",
    "3+ years of experience with Python",
    "Minimum of 2 years building APIs; 5-7 years preferred",
    "at least 4 yrs of backend work",
    "You bring 4 years of professional experience.",
    "Founded 40 years ago. 1+ year experience",
)


def _migration(revision: str) -> ModuleType:
    path = next(
        (REPO_ROOT / "src" / "jobpulse" / "migrations" / "versions").glob(f"{revision}_*.py")
    )
    spec = importlib.util.spec_from_file_location(f"migration_{revision}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("text_", YEARS_SAMPLES)
def test_0005_reads_years_like_the_filters_do(text_: str | None) -> None:
    """The migration keeps its own copy of the extraction (migrations never import
    application code); it must agree with core."""
    assert _migration("0005").years_required(text_) == extract_years_required(text_)


def test_0005_backfills_years_and_adds_status_times_and_blocks(db_url: str) -> None:
    config = alembic_config(db_url)
    command.upgrade(config, "0004")
    engine = create_engine(db_url)
    insert = text(
        "INSERT INTO postings (id, source, external_id, company_name, title, locations, "
        "canonical_url, fingerprint, url, first_seen_at, last_seen_at, also_seen_on, raw, "
        "description_text) VALUES (:id, 'greenhouse', :id, 'Acme', 'Role', '[]', :id, 'f', "
        "'https://example.com', :seen, :seen, '[]', '{}', :description)"
    )
    seen = "2026-10-24 08:00:00.000000"
    with engine.begin() as connection:
        for posting_id, description in enumerate(
            ("3+ years of experience", None, "No years here", "5-7 years"), start=1
        ):
            connection.execute(insert, {"id": posting_id, "seen": seen, "description": description})
        connection.execute(
            text(
                "INSERT INTO llm_assessments (posting_id, input_hash, model, created_at) "
                "VALUES (1, 'h', 'm', :seen)"
            ),
            {"seen": seen},
        )
        connection.execute(text("UPDATE postings SET merged_into_id = 1 WHERE id = 4"))

    command.upgrade(config, "0005")

    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, years_required, status_changed_at FROM postings ORDER BY id")
        ).all()
        assert [tuple(row) for row in rows] == [
            (1, 3, None),
            (2, None, None),
            (3, None, None),
            (4, 5, None),
        ]
        # columns added in place: no foreign-key actions
        assert connection.execute(text("SELECT count(*) FROM llm_assessments")).scalar() == 1
        merged = text("SELECT merged_into_id FROM postings WHERE id = 4")
        assert connection.execute(merged).scalar() == 1
    assert "blocked_companies" in inspect(engine).get_table_names()

    command.downgrade(config, "0004")
    columns = {c["name"] for c in inspect(engine).get_columns("postings")}
    assert not {"years_required", "status_changed_at"} & columns
    assert "blocked_companies" not in inspect(engine).get_table_names()


EMPLOYMENT_SAMPLES: tuple[tuple[str, dict[str, Any], str, str | None], ...] = (
    ("ashby", {"employmentType": "Contract"}, "Backend Engineer", None),
    ("ashby", {"employmentType": "Intern"}, "Backend Engineer", None),
    ("ashby", {"employmentType": "FullTime"}, "Backend Engineer", "A contract role."),
    ("lever", {"categories": {"commitment": "Temporary/Contract"}}, "Backend Engineer", None),
    ("lever", {"categories": {"commitment": "International EOR"}}, "Engineer (Contract)", None),
    ("lever", {"categories": None}, "Backend Engineer", None),
    ("indeed", {"job_type": "parttime, fulltime"}, "Backend Engineer", None),
    ("indeed", {"job_type": "fulltime, contract"}, "Backend Engineer", None),
    ("workday", {"timeType": "Part time"}, "Backend Engineer", None),
    (
        "greenhouse",
        {"metadata": [{"name": "Regular or Temporary", "value": "Temporary"}]},
        "Backend Engineer",
        None,
    ),
    ("greenhouse", {"metadata": [{"name": "Employment Type", "value": ["Intern"]}]}, "SWE", None),
    ("greenhouse", {"metadata": "odd"}, "Backend Engineer", None),
    ("linkedin", {}, "Backend Engineer", "Build.\nEmployment type: Contract\nIndustries: IT"),
    ("simplify", {}, "Software Engineer Intern", None),
    ("simplify", {}, "Smart Contract Engineer", "Write smart contracts."),
    ("simplify", {}, "Contract Analyst", None),
    ("simplify", {}, "Contract Software Engineer", None),
    ("simplify", {}, "Backend Engineer", "This is a 6-month contract."),
    ("simplify", {}, "Backend Engineer", "Open to C2C or 1099 contractors."),
    ("simplify", {}, "Backend Engineer", None),
)


@pytest.mark.parametrize(("source", "raw", "title", "description"), EMPLOYMENT_SAMPLES)
def test_0006_reads_employment_like_core(
    source: str, raw: dict[str, Any], title: str, description: str | None
) -> None:
    migration = _migration("0006")
    assert migration.employment_type(source, raw, title, description) == employment_type(
        source, raw, title, description
    )
    assert migration.requisition(source, raw) == requisition(source, raw)
    assert migration.requisition("greenhouse", {"internal_job_id": 77}) == requisition(
        "greenhouse", {"internal_job_id": 77}
    )


def test_0006_backfills_employment_and_reposts_in_place(db_url: str) -> None:
    config = alembic_config(db_url)
    command.upgrade(config, "0005")
    engine = create_engine(db_url)
    insert = text(
        "INSERT INTO postings (id, source, external_id, company_id, company_name, title, "
        "locations, canonical_url, fingerprint, url, first_seen_at, last_seen_at, also_seen_on, "
        "raw, tier, status) VALUES (:id, :source, :id, :company, 'Acme', 'Backend Engineer', "
        "'[]', :id, :fingerprint, 'https://example.com', :seen, :seen, '[]', :raw, :tier, "
        ":status)"
    )
    rows = [
        # id, source, company, fingerprint, raw, first seen (October), tier, status
        (1, "ashby", None, "a", '{"employmentType": "Contract"}', 1, "instant", "New"),
        (2, "greenhouse", 1, "b", '{"internal_job_id": 77}', 1, "baseline", "New"),
        (3, "greenhouse", 1, "c", '{"internal_job_id": 77}', 12, "instant", "New"),
        (4, "lever", None, "fp", "{}", 1, "baseline", "New"),
        (5, "lever", None, "fp", "{}", 5, "instant", "New"),
        (6, "lever", None, "fp", "{}", 20, "filtered", "Applied"),
        (7, "lever", None, "fp", "{}", 25, "baseline", "New"),
    ]
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO companies (id, name, ats_type, token, created_at, updated_at) "
                "VALUES (1, 'Acme', 'greenhouse', 'acme', :at, :at)"
            ),
            {"at": "2026-10-01 00:00:00.000000"},
        )
        for row_id, source, company, fingerprint, raw, day, tier, status in rows:
            connection.execute(
                insert,
                {
                    "id": row_id,
                    "source": source,
                    "company": company,
                    "fingerprint": fingerprint,
                    "raw": raw,
                    "seen": f"2026-10-{day:02d} 08:00:00.000000",
                    "tier": tier,
                    "status": status,
                },
            )
        connection.execute(
            text(
                "INSERT INTO llm_assessments (posting_id, input_hash, model, created_at) "
                "VALUES (3, 'h', 'm', '2026-10-12 08:00:00.000000')"
            )
        )

    command.upgrade(config, "0006")

    with engine.connect() as connection:
        found = connection.execute(
            text("SELECT id, employment_type, repost_reason FROM postings ORDER BY id")
        ).all()
        assert [tuple(row) for row in found] == [
            (1, "contract", None),
            (2, "full_time", None),
            (3, "full_time", "first seen 11 days ago (requisition 77)"),
            (4, "full_time", None),
            (5, "full_time", None),  # 4 days after the first: not a repost
            (6, "full_time", None),  # the same title and place alone is not a repost
            (7, "full_time", None),  # a baseline row never shows, so it isn't marked
        ]
        assert connection.execute(text("SELECT count(*) FROM llm_assessments")).scalar() == 1

    command.downgrade(config, "0005")
    columns = {c["name"] for c in inspect(engine).get_columns("postings")}
    assert not {"employment_type", "repost_reason"} & columns


def test_0007_adds_saved_filters_and_decided_at_in_place(db_url: str) -> None:
    config = alembic_config(db_url)
    command.upgrade(config, "0006")
    engine = create_engine(db_url)
    seen = "2026-10-24 08:00:00.000000"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO postings (id, source, external_id, company_name, title, locations, "
                "canonical_url, fingerprint, url, first_seen_at, last_seen_at, also_seen_on, raw) "
                "VALUES (1, 'greenhouse', '1', 'Acme', 'Role', '[]', 'c', 'f', 'u', :seen, :seen, "
                "'[]', '{}')"
            ),
            {"seen": seen},
        )
        connection.execute(
            text(
                "INSERT INTO llm_assessments (posting_id, input_hash, model, created_at) "
                "VALUES (1, 'h', 'm', :seen)"
            ),
            {"seen": seen},
        )

    command.upgrade(config, "0007")

    tables = set(inspect(engine).get_table_names())
    assert {"saved_filters", "filter_pushes", "preferences"} <= tables
    assert "decided_at" in {c["name"] for c in inspect(engine).get_columns("postings")}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM llm_assessments")).scalar() == 1

    command.downgrade(config, "0006")
    tables = set(inspect(engine).get_table_names())
    assert not {"saved_filters", "filter_pushes", "preferences"} & tables
    assert "decided_at" not in {c["name"] for c in inspect(engine).get_columns("postings")}


def test_0008_adds_resume_tables_and_columns_in_place(db_url: str) -> None:
    config = alembic_config(db_url)
    command.upgrade(config, "0007")
    engine = create_engine(db_url)
    seen = "2026-10-24 08:00:00.000000"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO postings (id, source, external_id, company_name, title, locations, "
                "canonical_url, fingerprint, url, first_seen_at, last_seen_at, also_seen_on, raw) "
                "VALUES (1, 'greenhouse', '1', 'Acme', 'Role', '[]', 'c', 'f', 'u', :seen, :seen, "
                "'[]', '{}')"
            ),
            {"seen": seen},
        )
        connection.execute(
            text(
                "INSERT INTO llm_assessments (posting_id, input_hash, model, created_at) "
                "VALUES (1, 'h', 'm', :seen)"
            ),
            {"seen": seen},
        )
        connection.execute(
            text(
                "INSERT INTO filter_pushes (id, posting_id, filter_id, sent_at) "
                "VALUES (1, 1, 1, :seen)"
            ),
            {"seen": seen},
        )

    command.upgrade(config, "0008")

    tables = set(inspect(engine).get_table_names())
    assert {"resume", "posting_vectors"} <= tables
    columns = {c["name"] for c in inspect(engine).get_columns("postings")}
    assert {"resume_score", "resume_line"} <= columns
    with engine.connect() as connection:  # in place: nothing cascaded
        assert connection.execute(text("SELECT count(*) FROM llm_assessments")).scalar() == 1
        assert connection.execute(text("SELECT count(*) FROM filter_pushes")).scalar() == 1
    with engine.begin() as connection:  # one resume at most
        insert = text(
            "INSERT INTO resume (id, filename, media_type, text, sha256, uploaded_at, lines, "
            "anchor_count) VALUES (:id, 'r.txt', 'text/plain', 't', 's', :seen, '[]', 0)"
        )
        connection.execute(insert, {"id": 1, "seen": seen})
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert, {"id": 2, "seen": seen})

    command.downgrade(config, "0007")
    tables = set(inspect(engine).get_table_names())
    assert not {"resume", "posting_vectors"} & tables
    columns = {c["name"] for c in inspect(engine).get_columns("postings")}
    assert not {"resume_score", "resume_line"} & columns
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM llm_assessments")).scalar() == 1


def test_0009_adds_the_profile_and_secrets_tables(db_url: str) -> None:
    config = alembic_config(db_url)
    command.upgrade(config, "0008")
    command.upgrade(config, "0009")
    engine = create_engine(db_url)
    tables = set(inspect(engine).get_table_names())
    assert {"profile", "secrets"} <= tables
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO profile (id, data, revision, created_at, updated_at) "
                "VALUES (1, '{}', 1, '2026-11-02 00:00:00', '2026-11-02 00:00:00')"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO profile (id, data, revision, created_at, updated_at) "
                "VALUES (2, '{}', 1, '2026-11-02 00:00:00', '2026-11-02 00:00:00')"
            )
        )
    command.downgrade(config, "0008")
    assert not {"profile", "secrets"} & set(inspect(engine).get_table_names())


def test_0010_adds_the_seed_state_table(db_url: str) -> None:
    config = alembic_config(db_url)
    command.upgrade(config, "0009")
    command.upgrade(config, "0010")
    engine = create_engine(db_url)
    assert "seed_state" in set(inspect(engine).get_table_names())
    row = "INSERT INTO seed_state (id, url, checked_at) VALUES ({id}, 'https://x', '2026-11-01')"
    with engine.begin() as connection:
        connection.execute(text(row.format(id=1)))
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text(row.format(id=2)))
    command.downgrade(config, "0009")
    assert "seed_state" not in set(inspect(engine).get_table_names())


def test_cli_db_upgrade_uses_database_url(tmp_path: Path, db_url: str) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("contact_email: dev@example.com\n", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "--env-file", str(tmp_path / ".env"), "db", "upgrade"],
        env={"DATABASE_URL": db_url},
    )
    assert result.exit_code == 0, result.output
    assert set(inspect(create_engine(db_url)).get_table_names()) >= TABLES


def test_0011_adds_agency_kinds_and_the_worker_lease(db_url: str) -> None:
    config = alembic_config(db_url)
    command.upgrade(config, "0010")
    engine = create_engine(db_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO blocked_companies (name, name_key, created_at) "
                "VALUES ('Jobot', 'jobot', '2026-11-01')"
            )
        )
    command.upgrade(config, "0011")
    with engine.begin() as connection:
        assert connection.execute(text("SELECT kind FROM blocked_companies")).scalar() == (
            "company"
        )
    assert "worker_lease" in set(inspect(engine).get_table_names())
    row = (
        "INSERT INTO worker_lease (id, holder, acquired_at, expires_at) "
        "VALUES ({id}, 'h:1:x', '2026-11-01', '2026-11-01')"
    )
    with engine.begin() as connection:
        connection.execute(text(row.format(id=1)))
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text(row.format(id=2)))
    command.downgrade(config, "0010")
    assert "worker_lease" not in set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        assert connection.execute(text("SELECT name FROM blocked_companies")).scalar() == "Jobot"
