"""M7: the board seed. Applying it adds the boards you lack as silent-first-poll `seed` rows
and skips known, blocked and paused ones; tombstones pause only seed boards nobody edited;
the daily remote sync is conditional, validated, capped and all-or-nothing on bad input;
`hide_agencies` keeps staffing agencies out everywhere the blocklist reaches."""

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
import time_machine
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpulse.config import AppConfig, SeedConfig, company_blocklist, filter_rules
from jobpulse.db import repo
from jobpulse.db.session import session_scope
from jobpulse.db.tables import Company, Run
from jobpulse.httpclient import HttpClient
from jobpulse.runtime import WATCHLIST_SOURCES, build_seed_sync
from jobpulse.seed import (
    Seed,
    SeedBoard,
    SeedPlan,
    SeedSync,
    plan_seed,
    render_seed,
    write_seed,
)
from jobpulse.sources.ashby import AshbyAdapter
from jobpulse.sources.ats_detect import AtsDetector
from jobpulse.sources.greenhouse import GreenhouseAdapter
from jobpulse.sources.lever import LeverAdapter
from tests.factories import NOW, jobspy_frame, jobspy_row
from tests.integration.conftest import WATCHLIST, Env, ScriptedScrape, add_watchlist, make_config

pytestmark = pytest.mark.respx(assert_all_called=False)

SEED_URL = "https://seed.example.com/boards.json"
WD_POSTMAN = "workday/postman.wd108/careers"
WD_POSTMAN_API = "https://postman.wd108.myworkdayjobs.com/wday/cxs/postman/careers/jobs"
WD_NVIDIA_API = "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"


def gh(token: str, name: str | None = None) -> SeedBoard:
    return SeedBoard("greenhouse", token, name or token.title())


def seed_text(*boards: SeedBoard, removed: tuple[tuple[str, str], ...] = ()) -> str:
    return render_seed(Seed(boards, removed))


def stored(sessions: sessionmaker[Session]) -> dict[str, Company]:
    with sessions() as session:
        return {f"{c.ats_type}/{c.token}": c for c in session.scalars(select(Company))}


def insert(sessions: sessionmaker[Session], *rows: dict[str, Any]) -> None:
    with sessions() as session:
        for row in rows:
            session.add(
                Company(
                    **{
                        "source_of_discovery": "seed",
                        "created_at": NOW - timedelta(days=3),
                        "updated_at": NOW - timedelta(days=3),
                        **row,
                    }
                )
            )
        session.commit()


async def apply(
    sessions: sessionmaker[Session],
    seed: Seed,
    detector: AtsDetector | None = None,
    config: AppConfig | None = None,
) -> tuple[SeedPlan, list[str], list[str]]:
    """What `jobpulse companies seed apply` does."""
    config = config or make_config()
    with sessions() as session:
        known = repo.known_companies(session)
        blocklist = repo.blocklist(
            session, company_blocklist(config), agencies=config.hide_agencies
        )
    plan = await plan_seed(
        seed,
        known=known,
        blocklist=blocklist,
        config=config,
        rules=filter_rules(config),
        detector=detector,
    )
    with session_scope(sessions) as session:
        added, retired = write_seed(session, plan, now=NOW)
    return plan, added, retired


@pytest.fixture
def detector(http: HttpClient) -> AtsDetector:
    return AtsDetector(http)


# --- applying ---------------------------------------------------------------------------------


async def test_new_boards_become_seed_rows_and_known_blocked_paused_ones_are_skipped(
    database: sessionmaker[Session],
) -> None:
    add_watchlist(database, *WATCHLIST)  # greenhouse/acme, lever/beta
    insert(database, {"name": "Clerk", "ats_type": "ashby", "token": "clerk", "active": False})
    with database() as session:
        repo.block_company(session, "Jobot", now=NOW)
        session.commit()
    seed = Seed(
        (
            gh("ACME", "Acme"),  # known, in another case
            gh("newco", "NewCo"),
            SeedBoard("lever", "olx", "OLX", "eu"),
            SeedBoard("ashby", "clerk", "Clerk"),  # paused here
            gh("jobot", "Jobot"),  # blocked on the dashboard
            SeedBoard("ashby", "talenthop", "TalentHop"),  # a staffing agency
            gh("spacex", "SpaceX"),  # a board on the profile's blocklist
        )
    )
    config = make_config(blocklist={"boards": ["greenhouse/spacex"]})

    plan, added, retired = await apply(database, seed, config=config)

    assert added == ["greenhouse/newco", "lever/olx"]
    assert retired == []
    assert plan.known == 2
    assert {r.board.label: r.outcome for r in plan.results if r.outcome != "add"} == {
        "greenhouse/jobot": "blocked",
        "ashby/talenthop": "blocked",
        "greenhouse/spacex": "blocked",
    }
    rows = stored(database)
    assert rows["lever/olx"].region == "eu"
    for label in added:
        row = rows[label]
        assert (row.source_of_discovery, row.priority, row.active) == ("seed", 0, True)
        assert (row.baseline_done_at, row.params, row.discovered_from) == (None, None, None)
    assert rows["ashby/clerk"].active is False  # still paused

    again, added_again, _ = await apply(database, seed, config=config)
    assert added_again == []
    assert again.known == 4
    assert len(stored(database)) == 5

    # with hide_agencies off, the agency is just another board
    _, added_agency, _ = await apply(
        database, seed, config=config.model_copy(update={"hide_agencies": False})
    )
    assert added_agency == ["ashby/talenthop"]


def wd_answer(request: httpx.Request, totals: dict[str, int]) -> httpx.Response:
    keyword = json.loads(request.content)["searchText"]
    return httpx.Response(
        200, json={"total": totals[keyword], "jobPostings": [{"title": "Customer Care Rep"}]}
    )


async def test_workday_seed_boards_keep_only_keywords_a_poll_can_read(
    database: sessionmaker[Session], detector: AtsDetector, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(WD_POSTMAN_API).mock(
        side_effect=lambda request: wd_answer(
            request,
            {
                "software engineer": 16,
                "full stack": 600,  # more than the 500 jobs a poll reads
                "backend engineer": 5,
                "machine learning engineer": 30,  # two pages, nothing that passes the title rules
            },
        )
    )
    respx_mock.post(WD_NVIDIA_API).mock(
        side_effect=lambda request: wd_answer(request, dict.fromkeys(
            ("software engineer", "full stack", "backend engineer", "machine learning engineer"),
            1500,
        ))
    )  # fmt: skip
    seed = Seed(
        (
            SeedBoard("workday", "postman.wd108/careers", "Postman"),
            SeedBoard("workday", "nvidia.wd5/NVIDIAExternalCareerSite", "NVIDIA"),
        )
    )
    plan, added, _ = await apply(database, seed, detector)

    assert added == [WD_POSTMAN]
    assert stored(database)[WD_POSTMAN].params == {
        "keywords": ["software engineer", "backend engineer"]
    }
    (broad,) = [r for r in plan.results if r.outcome != "add"]
    assert (broad.board.token, broad.outcome) == (
        "nvidia.wd5/NVIDIAExternalCareerSite",
        "too broad",
    )


async def test_a_tombstone_pauses_only_seed_boards_nobody_edited(
    database: sessionmaker[Session],
) -> None:
    insert(
        database,
        {"name": "Gone", "ats_type": "greenhouse", "token": "gone"},
        {"name": "Kept", "ats_type": "greenhouse", "token": "kept", "priority": 3},
        {"name": "Mine", "ats_type": "greenhouse", "token": "mine", "source_of_discovery": "auto"},
    )
    removed = (("greenhouse", "gone"), ("greenhouse", "kept"), ("greenhouse", "mine"))

    _, added, retired = await apply(database, Seed((), removed))

    assert (added, retired) == ([], ["greenhouse/gone"])
    assert {label: row.active for label, row in stored(database).items()} == {
        "greenhouse/gone": False,
        "greenhouse/kept": True,  # you gave it a priority
        "greenhouse/mine": True,  # not the seed's
    }


async def test_seed_boards_are_not_counted_against_discoverys_cap(
    database: sessionmaker[Session],
) -> None:
    await apply(database, Seed((gh("newco"), gh("other"))))
    with database() as session:
        assert repo.count_polled_auto_companies(session, company_blocklist(make_config())) == 0


# --- the daily remote sync --------------------------------------------------------------------


def remote(env: Env, **settings: Any) -> None:
    env.seed_sync = SeedSync(
        env.http, AtsDetector(env.http), SeedConfig(remote=True, url=SEED_URL, **settings)
    )


async def watchlist_cycle(env: Env, **kwargs: Any) -> Any:
    return await env.poll(sources=WATCHLIST_SOURCES, **kwargs)


def seed_runs(env: Env) -> list[Run]:
    return [run for run in env.runs() if run.source == "seed"]


@pytest.fixture
def quiet(env: Env) -> Env:
    """No watchlist boards to poll: a cycle is just its phases."""
    env.pause("acme", "beta")
    return env


async def test_a_daily_sync_adds_validated_boards_and_then_asks_with_its_etag(
    quiet: Env, respx_mock: respx.MockRouter, time_machine: time_machine.TimeMachineFixture
) -> None:
    env = quiet
    remote(env)
    route = respx_mock.get(SEED_URL)
    route.return_value = httpx.Response(
        200,
        text=seed_text(gh("acme", "Acme"), gh("newco", "NewCo"), gh("deadco", "DeadCo")),
        headers={"ETag": '"v1"'},
    )
    respx_mock.get(GreenhouseAdapter.list_url("newco")).respond(
        json={"jobs": [{"id": 1, "title": "Backend Engineer", "company_name": "NewCo"}]}
    )
    respx_mock.get(GreenhouseAdapter.list_url("deadco")).respond(404, json={"error": "nope"})

    report = await watchlist_cycle(env)

    assert report.seed.added == ["greenhouse/newco"]
    assert report.seed.skipped == [
        'greenhouse/deadco: invalid (HTTP 404 from boards-api.greenhouse.io: {"error":"nope"})'
    ]
    newco = env.company("newco")
    assert (newco.source_of_discovery, newco.baseline_done_at) == ("seed", None)
    with env.sessions() as session:
        state = repo.seed_state(session)
        assert state is not None
        assert (state.url, state.etag, state.checked_at, state.synced_at) == (
            SEED_URL, '"v1"', NOW, NOW,
        )  # fmt: skip
    (run,) = seed_runs(env)
    assert (run.target, run.ok, run.fetched_count, run.new_count) == ("remote", True, 3, 1)

    await watchlist_cycle(env)  # the same day: not due
    assert route.call_count == 1

    time_machine.move_to(NOW + timedelta(days=1), tick=False)
    route.return_value = httpx.Response(304)
    report = await watchlist_cycle(env)
    assert route.calls.last.request.headers["If-None-Match"] == '"v1"'
    assert report.seed.not_modified is True
    assert [(run.ok, run.not_modified) for run in seed_runs(env)] == [(True, False), (True, True)]


async def test_a_remote_tombstone_pauses_a_seed_board_nobody_edited(
    quiet: Env, respx_mock: respx.MockRouter
) -> None:
    env = quiet
    remote(env)
    insert(env.sessions, {"name": "Gone", "ats_type": "greenhouse", "token": "gone"})
    respx_mock.get(SEED_URL).respond(text=seed_text(removed=(("greenhouse", "gone"),)))

    report = await watchlist_cycle(env)

    assert report.seed.retired == ["greenhouse/gone"]
    assert env.company("gone").active is False


@pytest.mark.parametrize(
    ("body", "settings", "problem"),
    [
        ("not json", {}, "not JSON"),
        (json.dumps({"version": 9, "boards": []}), {}, "version 1"),
        (seed_text(gh("newco")), {"max_bytes": 10}, "more than seed.max_bytes (10)"),
    ],
)
async def test_bad_input_applies_nothing(
    quiet: Env, respx_mock: respx.MockRouter, body: str, settings: dict[str, Any], problem: str
) -> None:
    env = quiet
    remote(env, **settings)
    respx_mock.get(SEED_URL).respond(text=body, headers={"ETag": '"bad"'})
    validation = respx_mock.get(GreenhouseAdapter.list_url("newco"))

    report = await watchlist_cycle(env)

    assert problem in (report.seed.problem or "")
    assert not validation.called
    assert [c.token for c in env.companies()] == ["acme", "beta"]
    with env.sessions() as session:
        assert repo.seed_state(session) is None
    (run,) = seed_runs(env)
    assert (run.ok, run.error_kind) == (False, "parse")


async def test_a_failed_fetch_is_a_failed_run(quiet: Env, respx_mock: respx.MockRouter) -> None:
    env = quiet
    remote(env)
    respx_mock.get(SEED_URL).respond(404, text="Not Found")

    report = await watchlist_cycle(env)

    assert report.seed.problem == "HTTP 404 from seed.example.com: Not Found"
    (run,) = seed_runs(env)
    assert (run.ok, run.error_kind) == (False, "http_4xx")


async def test_new_boards_past_the_cap_wait_for_the_next_sync(
    quiet: Env, respx_mock: respx.MockRouter, time_machine: time_machine.TimeMachineFixture
) -> None:
    env = quiet
    remote(env, max_new_boards=1)
    route = respx_mock.get(SEED_URL)
    route.respond(text=seed_text(gh("first"), gh("second")), headers={"ETag": '"v1"'})
    for token in ("first", "second"):
        respx_mock.get(GreenhouseAdapter.list_url(token)).respond(json={"jobs": []})

    report = await watchlist_cycle(env)
    assert (report.seed.added, report.seed.capped) == (["greenhouse/first"], 1)
    with env.sessions() as session:
        state = repo.seed_state(session)
        assert state is not None
        assert state.etag is None

    time_machine.move_to(NOW + timedelta(days=1), tick=False)
    report = await watchlist_cycle(env)
    assert "If-None-Match" not in route.calls.last.request.headers
    assert (report.seed.added, report.seed.capped) == (["greenhouse/second"], 0)


async def test_no_sync_in_a_dry_run_a_job_board_cycle_or_with_remote_off(
    quiet: Env, respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    env = quiet
    remote(env)
    route = respx_mock.get(SEED_URL).respond(text=seed_text())

    assert (await watchlist_cycle(env, dry_run=True)).seed is None
    assert (await env.poll(sources=["linkedin"])).seed is None
    assert (await watchlist_cycle(env, target_key="acme")).seed is None
    assert not route.called
    assert seed_runs(env) == []
    assert build_seed_sync(http, make_config()) is None  # seed.remote is off by default


async def test_remote_boards_are_validated_and_their_own_name_checked(
    quiet: Env, respx_mock: respx.MockRouter
) -> None:
    """A board whose API names a blocked company stays out, whatever the file calls it."""
    env = quiet
    remote(env)
    respx_mock.get(SEED_URL).respond(
        text=seed_text(gh("hopco", "Hop Co"), SeedBoard("lever", "olx", "OLX"))
    )
    respx_mock.get(GreenhouseAdapter.list_url("hopco")).respond(
        json={"jobs": [{"id": 1, "title": "Recruiter", "company_name": "TalentHop"}]}
    )
    respx_mock.get(LeverAdapter.list_url("olx")).respond(404, json={"ok": False})
    respx_mock.get(LeverAdapter.list_url("olx", "eu")).respond(json=[])

    report = await watchlist_cycle(env)

    assert report.seed.added == ["lever/olx"]
    assert env.company("olx").region == "eu"  # the region that answered


# --- hide_agencies ----------------------------------------------------------------------------


async def test_agencies_are_hidden_unless_hide_agencies_is_off(
    env: Env, boards: ScriptedScrape, respx_mock: respx.MockRouter
) -> None:
    env.pause("acme", "beta")
    env.watch("Nova Talent", "ashby", "nova-talent")
    board = respx_mock.get(AshbyAdapter.list_url("nova-talent")).respond(json={"jobs": []})
    env.add_search()
    rows = [
        jobspy_row("linkedin", external_id="4101000071", company="Motion Recruitment"),
        jobspy_row("linkedin", external_id="4101000072", company="Northwind"),
    ]
    boards.queue("linkedin", jobspy_frame([]), jobspy_frame(rows))
    await env.poll(sources=["linkedin"])
    report = await env.poll(sources=["linkedin"])

    assert report.pushes == ["Northwind · Backend Engineer · SF · posted today"]
    assert env.posting("linkedin", "4101000071").tier_reason == "company is on the blocklist"

    await env.poll(sources=["ashby"])
    assert not board.called  # an agency's board isn't polled
    env.reconfigure(hide_agencies=False)
    await env.poll(sources=["ashby"])
    assert board.called
