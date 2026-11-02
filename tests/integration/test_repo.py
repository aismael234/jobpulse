import dataclasses
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from jobpulse.config import SIMPLIFY_NEW_GRAD_URL, SearchEntry, SimplifyConfig, WatchlistEntry
from jobpulse.core.applicants import ApplicantCount
from jobpulse.core.assessment import Assessment
from jobpulse.core.models import Decision
from jobpulse.db import repo
from jobpulse.db.session import session_scope
from jobpulse.db.tables import Company, PostingRow, Run, Search, SourceState
from jobpulse.llm.base import LlmReply
from tests.factories import NOW, make_posting

WATCHLIST: tuple[dict[str, object], ...] = (
    {"name": "Acme", "ats": "greenhouse", "token": "acme", "priority": 5},
    {"name": "Beta", "ats": "lever", "token": "beta", "priority": 3},
)


def watchlist(*entries: dict[str, object]) -> list[WatchlistEntry]:
    """An old config.yaml's watchlist entries, as `profile import` reads them."""
    return [WatchlistEntry.model_validate(entry) for entry in (entries or WATCHLIST)]


def test_watchlist_import_is_idempotent(database: sessionmaker[Session]) -> None:
    with session_scope(database) as session:
        first = repo.import_watchlist(session, watchlist(), now=NOW)
    with session_scope(database) as session:
        second = repo.import_watchlist(session, watchlist(), now=NOW)

    assert first.added == ("greenhouse/acme", "lever/beta")
    assert second == repo.SyncReport()
    with database() as session:
        assert set(session.scalars(select(Company.source_of_discovery))) == {"dashboard"}
        assert session.scalar(select(func.count(Company.id))) == 2


def test_import_updates_changed_fields_and_leaves_other_companies_alone(
    database: sessionmaker[Session],
) -> None:
    """The dashboard owns every company now: one left out of the file isn't paused."""
    with session_scope(database) as session:
        repo.import_watchlist(session, watchlist(), now=NOW)
    changed = watchlist({"name": "Acme Corp", "ats": "greenhouse", "token": "acme", "priority": 9})
    with session_scope(database) as session:
        report = repo.import_watchlist(session, changed, now=NOW)

    assert report.updated == ("greenhouse/acme",)
    with database() as session:
        acme = session.scalars(select(Company).where(Company.token == "acme")).one()
        beta = session.scalars(select(Company).where(Company.token == "beta")).one()
    assert (acme.name, acme.priority, acme.active) == ("Acme Corp", 9, True)
    assert beta.active is True


def test_rows_config_yaml_owned_become_dashboard_rows(database: sessionmaker[Session]) -> None:
    with session_scope(database) as session:
        session.add(
            Company(
                name="Gamma",
                ats_type="ashby",
                token="gamma",
                active=False,
                source_of_discovery="config",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    with session_scope(database) as session:
        report = repo.import_watchlist(session, watchlist(), now=NOW)
    assert report.updated == ("ashby/gamma",)
    with database() as session:
        gamma = session.scalars(select(Company).where(Company.token == "gamma")).one()
    assert (gamma.source_of_discovery, gamma.active) == ("dashboard", False)


def test_blocked_names_import_once(database: sessionmaker[Session]) -> None:
    with session_scope(database) as session:
        assert repo.import_blocklist(session, ["Jobot", "Revature LLC"], now=NOW) == [
            "Jobot",
            "Revature LLC",
        ]
    with session_scope(database) as session:
        assert repo.import_blocklist(session, ["JOBOT", "Revature"], now=NOW) == []
        assert len(repo.blocked_companies(session)) == 2


def test_list_companies(database: sessionmaker[Session]) -> None:
    with session_scope(database) as session:
        repo.import_watchlist(session, watchlist(), now=NOW)
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
        repo.import_watchlist(session, watchlist(), now=NOW)
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


# --- run history retention ------------------------------------------------------------------


def test_delete_runs_before_removes_only_older_runs(database: sessionmaker[Session]) -> None:
    cutoff = NOW - timedelta(days=30)
    with session_scope(database) as session:
        for started in (cutoff - timedelta(days=2), cutoff - timedelta(seconds=1), cutoff, NOW):
            session.add(Run(source="greenhouse", target="acme", started_at=started, ok=True))
    with session_scope(database) as session:
        assert repo.delete_runs_before(session, cutoff) == 2
    with session_scope(database) as session:
        kept = sorted(session.scalars(select(Run.started_at)))
    assert kept == [cutoff, NOW]


# --- Milestone 4 ---------------------------------------------------------------------------

OMEGA = {"name": "Omega", "ats": "workday", "token": "omega.wd5/External"}


def workday_config(keywords: list[str]) -> list[WatchlistEntry]:
    return watchlist({**OMEGA, "keywords": keywords})


def test_workday_keywords_are_stored_and_reach_the_target(database: sessionmaker[Session]) -> None:
    with session_scope(database) as session:
        repo.import_watchlist(session, workday_config(["backend", "platform"]), now=NOW)
    with database() as session:
        company = session.scalars(select(Company)).one()
        [target] = repo.company_targets(session)
    assert company.params == {"keywords": ["backend", "platform"]}
    assert (target.ats, target.token, target.keywords) == (
        "workday",
        "omega.wd5/External",
        ("backend", "platform"),
    )


def test_changed_workday_keywords_start_a_new_baseline(database: sessionmaker[Session]) -> None:
    with session_scope(database) as session:
        repo.import_watchlist(session, workday_config(["backend"]), now=NOW)
        session.execute(update(Company).values(baseline_done_at=NOW))
    with session_scope(database) as session:
        unchanged = repo.import_watchlist(session, workday_config(["backend"]), now=NOW)
    with database() as session:
        assert session.scalars(select(Company)).one().baseline_done_at == NOW
    with session_scope(database) as session:
        changed = repo.import_watchlist(session, workday_config(["backend", "api"]), now=NOW)
    assert unchanged == repo.SyncReport()
    assert changed.updated == ("workday/omega.wd5/External",)
    with database() as session:
        company = session.scalars(select(Company)).one()
    assert company.baseline_done_at is None
    assert company.params == {"keywords": ["backend", "api"]}


def test_other_boards_have_no_keywords(database: sessionmaker[Session]) -> None:
    with session_scope(database) as session:
        repo.import_watchlist(session, watchlist(), now=NOW)
    with database() as session:
        targets = repo.company_targets(session)
    assert all(target.keywords == () for target in targets)


def test_a_dashboard_workday_board_keeps_its_keywords(database: sessionmaker[Session]) -> None:
    with session_scope(database) as session:
        assert repo.add_company(
            session,
            ats="workday",
            token="omega.wd5/External",
            region="us",
            name="Omega",
            source="dashboard",
            priority=2,
            discovered_from=None,
            now=NOW,
            params={"keywords": ["backend"]},
        )
    with database() as session:
        [target] = repo.company_targets(session)
    assert target.keywords == ("backend",)


def test_applicants_are_stored_with_the_decision_and_read_back(
    database: sessionmaker[Session],
) -> None:
    posting = make_posting(source="linkedin", external_id="4101100003", posted_at=None)
    with session_scope(database) as session:
        row_id = repo.insert_if_absent(
            session,
            posting,
            company_id=None,
            fingerprint="f",
            tier="pending",
            tier_reason=None,
            now=NOW,
        )
        assert row_id is not None
        count = ApplicantCount("Be among the first 25 applicants", None, 25)
        repo.save_evaluation(
            session,
            row_id,
            dataclasses.replace(posting, applicants=count),
            Decision(tier="instant", reason="score 0.50", score=0.5),
        )
    with database() as session:
        row = session.get(PostingRow, row_id)
        assert row is not None
        assert (row.applicant_label, row.applicant_min, row.applicant_max) == (
            "Be among the first 25 applicants",
            None,
            25,
        )
        assert repo.row_to_posting(row).applicants == count


def test_llm_answers_are_cached_per_posting_and_every_call_counts(
    database: sessionmaker[Session],
) -> None:
    with session_scope(database) as session:
        ids = [
            repo.insert_if_absent(
                session,
                make_posting(external_id=str(n)),
                company_id=None,
                fingerprint=str(n),
                tier="pending",
                tier_reason=None,
                now=NOW,
            )
            for n in range(3)
        ]
        first, second, third = (row_id for row_id in ids if row_id is not None)
        answer = Assessment("mid", 0.9, 0.7, "Python APIs.")
        repo.record_assessment(
            session,
            posting_id=first,
            input_hash="a" * 64,
            reply=LlmReply(
                model="claude-opus-5-5", assessment=answer, input_tokens=900, output_tokens=120
            ),
            now=NOW - timedelta(hours=25),
        )
        repo.record_assessment(
            session,
            posting_id=second,
            input_hash="b" * 64,
            reply=LlmReply(model="claude-opus-5-5", error="HTTP 529: overloaded"),
            now=NOW - timedelta(hours=1),
        )
    with database() as session:
        cached = repo.cached_assessments(
            session, {first: "a" * 64, second: "b" * 64, third: "c" * 64}
        )
        assert cached == {first: answer}  # failures are never answers
        assert repo.cached_assessments(session, {first: "z" * 64}) == {}  # another question
        assert repo.count_llm_calls(session, since=NOW - timedelta(hours=24)) == 1
        assert repo.count_llm_calls(session, since=NOW - timedelta(hours=48)) == 2
