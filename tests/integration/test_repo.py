from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from jobpulse.config import SIMPLIFY_NEW_GRAD_URL, SearchEntry, SimplifyConfig
from jobpulse.db import repo
from jobpulse.db.session import session_scope
from jobpulse.db.tables import Company, Search, SourceState
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


# --- searches and feed state (Milestone 2) -------------------------------------------------


def searches(*entries: dict[str, object]) -> list[SearchEntry]:
    return [
        SearchEntry.model_validate({"site": "linkedin", "search_term": "backend engineer", **entry})
        for entry in entries
    ]


def test_search_sync_is_idempotent(database: sessionmaker[Session]) -> None:
    entries = searches(
        {"name": "backend-sf", "location": "San Francisco, CA", "results_wanted": 20}
    )
    with session_scope(database) as session:
        first = repo.sync_searches(session, entries, now=NOW)
    with session_scope(database) as session:
        second = repo.sync_searches(session, entries, now=NOW)
    assert first.added == ("backend-sf",)
    assert second == repo.SyncReport()
    with database() as session:
        search = session.scalars(select(Search)).one()
    assert (search.site, search.search_term, search.location, search.hours_old) == (
        "linkedin",
        "backend engineer",
        "San Francisco, CA",
        3,
    )
    assert search.params == {
        "results_wanted": 20,
        "is_remote": None,
        "country_indeed": "USA",
        "linkedin_company_ids": [],
    }


def test_a_changed_search_definition_resets_its_baseline(database: sessionmaker[Session]) -> None:
    with session_scope(database) as session:
        repo.sync_searches(session, searches({"name": "s"}), now=NOW)
        session.execute(update(Search).values(baseline_done_at=NOW))
    with session_scope(database) as session:
        report = repo.sync_searches(session, searches({"name": "s", "results_wanted": 50}), now=NOW)
    assert report.updated == ("s",)
    with database() as session:
        assert session.scalars(select(Search)).one().baseline_done_at == NOW  # same search

    with session_scope(database) as session:
        repo.sync_searches(session, searches({"name": "s", "search_term": "platform"}), now=NOW)
    with database() as session:
        assert session.scalars(select(Search)).one().baseline_done_at is None


def test_searches_removed_from_config_are_deactivated(database: sessionmaker[Session]) -> None:
    with session_scope(database) as session:
        repo.sync_searches(session, searches({"name": "a"}, {"name": "b"}), now=NOW)
    with session_scope(database) as session:
        report = repo.sync_searches(session, searches({"name": "a"}), now=NOW)
    assert report.deactivated == ("b",)
    with database() as session:
        active = {s.name: s.active for s in session.scalars(select(Search))}
    assert active == {"a": True, "b": False}


def test_feed_targets_keep_the_etag_and_reset_on_a_new_url(database: sessionmaker[Session]) -> None:
    feed = SimplifyConfig()
    with session_scope(database) as session:
        [target] = repo.feed_targets(session, feed)
        repo.save_feed_etag(session, target, etag='W/"1"', now=NOW)
        repo.mark_baseline_done(session, target, now=NOW)
    with session_scope(database) as session:
        [again] = repo.feed_targets(session, feed)
    assert (again.etag, again.baseline_done, again.url) == ('W/"1"', True, SIMPLIFY_NEW_GRAD_URL)

    moved = SimplifyConfig(url="https://raw.githubusercontent.com/example/list/main/listings.json")
    with session_scope(database) as session:
        [fresh] = repo.feed_targets(session, moved)
    assert (fresh.etag, fresh.baseline_done, fresh.url) == (None, False, moved.url)
    with database() as session:
        assert len(list(session.scalars(select(SourceState)))) == 1


def test_known_companies_cover_every_company_in_lowercase(database: sessionmaker[Session]) -> None:
    with session_scope(database) as session:
        repo.sync_watchlist(session, make_config().watchlist, now=NOW)
        session.add(
            Company(
                name="Sierra",
                ats_type="ashby",
                token="Sierra",
                active=False,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    with database() as session:
        assert repo.known_companies(session) == {
            ("greenhouse", "acme"),
            ("lever", "beta"),
            ("ashby", "sierra"),
        }
