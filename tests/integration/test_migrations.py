from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from typer.testing import CliRunner

from jobpulse.cli import app
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
