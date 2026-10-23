from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session

from jobpulse.config import AppConfig, Settings
from jobpulse.db.session import create_engine_for, resolve_database_url
from jobpulse.db.tables import Base, Company


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path}"


def test_aware_datetimes_round_trip_as_utc(tmp_path: Path) -> None:
    engine = create_engine_for(sqlite_url(tmp_path / "t.db"))
    Base.metadata.create_all(engine)
    pacific = timezone(timedelta(hours=-7))
    created = datetime(2026, 10, 23, 5, 30, tzinfo=pacific)

    with Session(engine) as session:
        session.add(
            Company(
                name="Acme", ats_type="lever", token="acme", created_at=created, updated_at=created
            )
        )
        session.commit()
    with Session(engine) as session:
        company = session.scalars(select(Company)).one()

    assert company.created_at == created
    assert company.created_at.utcoffset() == timedelta(0)
    assert company.created_at.tzinfo is UTC


def test_naive_datetimes_are_rejected(tmp_path: Path) -> None:
    engine = create_engine_for(sqlite_url(tmp_path / "t.db"))
    Base.metadata.create_all(engine)
    naive = datetime(2026, 10, 23, 12, 0)  # noqa: DTZ001 - the point of the test

    with Session(engine) as session:
        session.add(
            Company(name="Acme", ats_type="lever", token="acme", created_at=naive, updated_at=naive)
        )
        with pytest.raises(StatementError, match="naive datetime"):
            session.commit()


def test_sqlite_runs_in_wal_mode_with_foreign_keys(tmp_path: Path) -> None:
    engine = create_engine_for(sqlite_url(tmp_path / "t.db"))
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() >= 5000


def test_sqlite_parent_directory_is_created(tmp_path: Path) -> None:
    engine = create_engine_for(sqlite_url(tmp_path / "nested" / "dir" / "jobpulse.db"))
    with engine.connect():
        pass
    assert (tmp_path / "nested" / "dir" / "jobpulse.db").exists()


def test_database_url_precedence(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {"contact_email": "dev@example.com", "database_url": "sqlite:///from-config.db"}
    )
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=sqlite:///from-env.db\n", encoding="utf-8")

    assert resolve_database_url(Settings(_env_file=env_file), config) == "sqlite:///from-env.db"
    assert resolve_database_url(Settings(_env_file=None), config) == "sqlite:///from-config.db"
    assert resolve_database_url(Settings(_env_file=None), None) == "sqlite:///data/jobpulse.db"
