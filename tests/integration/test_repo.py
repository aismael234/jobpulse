from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from jobpulse.db import repo
from jobpulse.db.session import session_scope
from jobpulse.db.tables import Company
from tests.factories import NOW
from tests.integration.conftest import make_config


def test_watchlist_sync_is_idempotent(database: sessionmaker[Session]) -> None:
    entries = make_config().watchlist
    with session_scope(database) as session:
        first = repo.sync_watchlist(session, entries, now=NOW)
    with session_scope(database) as session:
        second = repo.sync_watchlist(session, entries, now=NOW)

    assert first.added == ("greenhouse/acme", "lever/beta")
    assert second == repo.SyncReport()
    with database() as session:
        assert session.scalar(select(func.count(Company.id))) == 2


def test_sync_updates_changed_fields_and_deactivates_removed_companies(
    database: sessionmaker[Session],
) -> None:
    with session_scope(database) as session:
        repo.sync_watchlist(session, make_config().watchlist, now=NOW)
    changed = make_config(
        watchlist=[{"name": "Acme Corp", "ats": "greenhouse", "token": "acme", "priority": 9}]
    ).watchlist
    with session_scope(database) as session:
        report = repo.sync_watchlist(session, changed, now=NOW)

    assert report.updated == ("greenhouse/acme",)
    assert report.deactivated == ("lever/beta",)
    with database() as session:
        acme = session.scalars(select(Company).where(Company.token == "acme")).one()
        beta = session.scalars(select(Company).where(Company.token == "beta")).one()
    assert (acme.name, acme.priority, acme.active) == ("Acme Corp", 9, True)
    assert beta.active is False


def test_companies_added_outside_config_are_left_alone(database: sessionmaker[Session]) -> None:
    with session_scope(database) as session:
        session.add(
            Company(
                name="Gamma",
                ats_type="ashby",
                token="gamma",
                source_of_discovery="dashboard",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    with session_scope(database) as session:
        report = repo.sync_watchlist(session, make_config().watchlist, now=NOW)
    assert report.deactivated == ()
    with database() as session:
        gamma = session.scalars(select(Company).where(Company.token == "gamma")).one()
    assert gamma.active is True


def test_list_companies(database: sessionmaker[Session]) -> None:
    with session_scope(database) as session:
        repo.sync_watchlist(session, make_config().watchlist, now=NOW)
    with session_scope(database) as session:
        overview = repo.list_companies(session)
    assert [(c.ats, c.token, c.open_postings, c.baseline_done_at) for c in overview] == [
        ("greenhouse", "acme", 0, None),
        ("lever", "beta", 0, None),
    ]
