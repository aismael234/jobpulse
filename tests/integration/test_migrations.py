from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect
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
