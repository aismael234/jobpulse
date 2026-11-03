"""Section 7, items 1–7, end to end: adapters over respx, a real SQLite database, the poller."""

from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx

from jobpulse.core.dedupe import posting_fingerprint
from jobpulse.db import repo
from jobpulse.notify.ntfy import NtfyNotifier
from tests.factories import NOW, make_posting
from tests.integration.conftest import (
    LEVER_LIST,
    Env,
    GreenhouseMock,
    board_posting,
    gh_job,
    lever_posting,
)

pytestmark = pytest.mark.respx(assert_all_called=False)

OLD = NOW - timedelta(days=3)
FRESH = NOW - timedelta(minutes=25)
TITLE_LINE = "Acme · Backend Engineer · SF / Remote · posted 25 min ago"
LEVER_ID = "5c6e2a0e-1f2b-4c3d-9e8f-0a1b2c3d4e5f"


async def baseline(env: Env, gh: GreenhouseMock, *job_ids: int) -> None:
    gh.respond(*(gh_job(job_id, published=OLD) for job_id in job_ids))
    await env.poll(sources=["greenhouse"])


# --- item 1: silent baseline ------------------------------------------------------------


async def test_first_poll_is_a_silent_baseline(env: Env, gh: GreenhouseMock) -> None:
    gh.respond(
        gh_job(101, published=NOW - timedelta(minutes=5)),
        gh_job(102, "Platform Engineer", published=NOW - timedelta(hours=1)),
        gh_job(103, published=NOW - timedelta(days=30)),
    )
    report = await env.poll(sources=["greenhouse"])

    assert env.notifier.sent == []
    rows = env.postings()
    assert len(rows) == 3
    assert {row.tier for row in rows} == {"baseline"}
    assert env.company("acme").baseline_done_at == NOW
    [target] = report.targets
    assert target.is_baseline
    assert target.baseline_stored == 3
    [run] = env.runs()
    assert run.ok
    assert run.fetched_count == 3
    assert run.new_count == 0


async def test_a_failed_first_poll_is_not_the_baseline(env: Env, gh: GreenhouseMock) -> None:
    gh.fail_with(httpx.Response(503))
    await env.poll(sources=["greenhouse"])
    assert env.postings() == []
    assert env.company("acme").baseline_done_at is None
    [run] = env.runs()
    assert not run.ok
    assert run.error_kind == "http_5xx"

    gh.respond(gh_job(101, published=NOW - timedelta(minutes=5)))
    await env.poll(sources=["greenhouse"])
    assert env.notifier.sent == []
    assert env.company("acme").baseline_done_at == NOW
    assert [row.tier for row in env.postings()] == ["baseline"]


# --- item 2: one new matching job, one push ----------------------------------------------


async def test_new_matching_job_sends_exactly_one_push_with_the_title_line(
    env: Env, gh: GreenhouseMock, respx_mock: respx.MockRouter
) -> None:
    topic = "jobpulse-Zq3vX9kR2mT7wL4pB8nC1dF6"
    ntfy = respx_mock.post(f"https://ntfy.sh/{topic}").respond(200, json={"id": "Xa1"})
    env.notifier = NtfyNotifier(env.http, base_url="https://ntfy.sh", topic=topic)
    await baseline(env, gh, 101)

    gh.respond(gh_job(101, published=OLD), gh_job(102, published=FRESH))
    report = await env.poll(sources=["greenhouse"])

    assert ntfy.call_count == 1
    assert ntfy.calls.last.request.headers["Title"] == "1 new job posting · watchlist"
    assert ntfy.calls.last.request.content.decode() == (
        "1 new job posting was found via watchlist. Top score: Acme - "
        f"{env.posting('greenhouse', '102').score:.2f}"
    )
    assert (
        ntfy.calls.last.request.headers["Click"] == "https://job-boards.greenhouse.io/acme/jobs/102"
    )
    assert report.pushes == [TITLE_LINE]
    row = env.posting("greenhouse", "102")
    assert row.tier == "instant"
    assert row.notified_at == NOW
    assert row.description_text is not None
    assert "Python" in row.description_text
    assert row.score is not None
    assert row.score_breakdown is not None

    await env.poll(sources=["greenhouse"])
    assert ntfy.call_count == 1


# --- item 3: freshness gate --------------------------------------------------------------


async def test_new_job_with_an_old_timestamp_is_stored_but_not_pushed(
    env: Env, gh: GreenhouseMock
) -> None:
    await baseline(env, gh, 101)
    gh.respond(gh_job(101, published=OLD), gh_job(102, published=NOW - timedelta(days=5)))
    await env.poll(sources=["greenhouse"])

    assert env.notifier.sent == []
    row = env.posting("greenhouse", "102")
    assert row.tier == "stale"
    assert row.tier_reason is not None
    assert "freshness" in row.tier_reason


# --- item 4: updated_at churn -----------------------------------------------------------


async def test_an_updated_at_change_creates_no_row_and_no_push(
    env: Env, gh: GreenhouseMock
) -> None:
    gh.respond(gh_job(101, published=FRESH, updated=FRESH))
    await env.poll(sources=["greenhouse"])
    gh.respond(gh_job(101, published=FRESH, updated=NOW))
    await env.poll(sources=["greenhouse"])

    assert len(env.postings()) == 1
    assert env.notifier.sent == []


# --- item 5: overlapping job-board windows ---------------------------------------------


async def test_overlapping_job_board_windows_create_no_duplicates(env: Env) -> None:
    env.add_search()
    env.board.results = [
        [board_posting("9001")],
        [board_posting("9001"), board_posting("9002", title="Backend Engineer, Data")],
        [board_posting("9002", title="Backend Engineer, Data")],
    ]
    for _ in range(3):
        await env.poll(sources=["linkedin"])

    assert sorted(row.external_id for row in env.postings()) == ["9001", "9002"]
    assert len(env.notifier.sent) == 1
    assert env.posting("linkedin", "9001").tier == "baseline"


# --- item 6: cross-source dedupe ----------------------------------------------------------


@pytest.mark.parametrize(
    ("direct_url", "primary"),
    [
        (
            "https://boards.greenhouse.io/acme/jobs/101?gh_src=abc123&utm_source=LinkedIn",
            ("greenhouse", "101"),
        ),
        ("https://careers.acme.example/jobs?gh_jid=101&gh_src=li", ("greenhouse", "101")),
        (f"https://jobs.lever.co/beta/{LEVER_ID}/apply?lever-source=LinkedIn", ("lever", LEVER_ID)),
    ],
    ids=["greenhouse-tracking-params", "custom-domain-gh_jid", "lever-apply-url"],
)
async def test_job_board_copy_of_an_ats_posting_is_merged_not_pushed(
    env: Env,
    gh: GreenhouseMock,
    respx_mock: respx.MockRouter,
    direct_url: str,
    primary: tuple[str, str],
) -> None:
    gh.respond(gh_job(101, published=OLD, url="https://careers.acme.example/jobs?gh_jid=101"))
    respx_mock.get(LEVER_LIST).respond(200, json=[lever_posting(LEVER_ID, created=OLD)])
    await env.poll(sources=["greenhouse", "lever"])

    env.add_search()
    env.board.results = [
        [],
        [
            board_posting(
                "9001", title="Backend Engineer (posted on LinkedIn)", direct_url=direct_url
            )
        ],
    ]
    await env.poll(sources=["linkedin"])
    await env.poll(sources=["linkedin"])

    assert env.notifier.sent == []
    duplicate = env.posting("linkedin", "9001")
    original = env.posting(*primary)
    assert duplicate.merged_into_id == original.id
    assert duplicate.tier == "merged"
    assert [(e["source"], e["external_id"]) for e in original.also_seen_on] == [
        ("linkedin", "9001")
    ]


async def test_fingerprint_fallback_merges_a_copy_without_an_ats_url(
    env: Env, gh: GreenhouseMock
) -> None:
    gh.respond(gh_job(101, published=OLD, location="San Francisco, CA"))
    await env.poll(sources=["greenhouse"])
    env.add_search()
    env.board.results = [
        [],
        [
            board_posting(
                "9001", company="Acme, Inc.", location="San Francisco, California, United States"
            )
        ],
    ]
    await env.poll(sources=["linkedin"])
    await env.poll(sources=["linkedin"])

    assert env.notifier.sent == []
    original = env.posting("greenhouse", "101")
    assert env.posting("linkedin", "9001").merged_into_id == original.id
    assert original.also_seen_on[0]["matched_by"] == "fingerprint"


async def test_fingerprint_match_outside_the_window_does_not_merge(
    env: Env, gh: GreenhouseMock
) -> None:
    gh.respond(gh_job(101, published=OLD, location="San Francisco, CA"))
    await env.poll(sources=["greenhouse"])
    env.update_posting("greenhouse", "101", last_seen_at=NOW - timedelta(days=31))
    env.add_search()
    env.board.results = [[], [board_posting("9001", company="Acme, Inc.")]]
    await env.poll(sources=["linkedin"])
    await env.poll(sources=["linkedin"])

    assert env.posting("linkedin", "9001").merged_into_id is None
    assert len(env.notifier.sent) == 1


# --- item 7: closures --------------------------------------------------------------------


async def test_job_missing_from_two_complete_polls_closes_then_reopens(
    env: Env, gh: GreenhouseMock
) -> None:
    jobs = {job_id: gh_job(job_id, published=OLD) for job_id in (101, 102, 103)}
    gh.respond(*jobs.values())
    await env.poll(sources=["greenhouse"])

    gh.respond(jobs[101], jobs[102])
    await env.poll(sources=["greenhouse"])
    after_one = env.posting("greenhouse", "103")
    assert after_one.missing_count == 1
    assert after_one.closed_at is None

    await env.poll(sources=["greenhouse"])
    after_two = env.posting("greenhouse", "103")
    assert after_two.missing_count == 2
    assert after_two.closed_at == NOW

    gh.respond(*jobs.values())
    await env.poll(sources=["greenhouse"])
    reopened = env.posting("greenhouse", "103")
    assert reopened.closed_at is None
    assert reopened.missing_count == 0
    assert len(env.postings()) == 3
    assert env.notifier.sent == []


FAILURES: dict[str, httpx.Response | Exception] = {
    "http-500": httpx.Response(500),
    "timeout": httpx.ReadTimeout("slow"),
    "malformed-json": httpx.Response(200, text="{not json"),
    "empty-list": httpx.Response(200, json={"jobs": [], "meta": {"total": 0}}),
}
EXPECTED_KINDS = {"http-500": "http_5xx", "timeout": "timeout", "malformed-json": "parse"}


@pytest.mark.parametrize("failure", list(FAILURES))
async def test_failed_or_empty_polls_close_nothing(
    env: Env, gh: GreenhouseMock, failure: str
) -> None:
    await baseline(env, gh, 101, 102)
    gh.fail_with(FAILURES[failure])
    for _ in range(3):
        await env.poll(sources=["greenhouse"])

    for row in env.postings():
        assert row.missing_count == 0
        assert row.closed_at is None
    later_runs = env.runs()[1:]
    assert len(later_runs) == 3
    if failure == "empty-list":
        assert all(run.ok and run.fetched_count == 0 for run in later_runs)
    else:
        assert all(not run.ok and run.error_kind == EXPECTED_KINDS[failure] for run in later_runs)


async def test_job_board_results_never_close_anything(env: Env) -> None:
    env.add_search()
    env.board.results = [[board_posting("9001"), board_posting("9002")]] + [
        [board_posting("9001")] for _ in range(3)
    ]
    for _ in range(4):
        await env.poll(sources=["linkedin"])
    for row in env.postings():
        assert row.closed_at is None
        assert row.missing_count == 0


# --- pushes, tiers and recovery ----------------------------------------------------------


async def test_a_failed_push_is_retried_next_cycle_without_duplicates(
    env: Env, gh: GreenhouseMock
) -> None:
    await baseline(env, gh, 101)
    env.notifier.failures_left = 1
    gh.respond(gh_job(101, published=OLD), gh_job(102, published=FRESH))

    first = await env.poll(sources=["greenhouse"])
    assert first.failed_pushes == 1
    assert env.posting("greenhouse", "102").notified_at is None

    second = await env.poll(sources=["greenhouse"])
    assert second.pushes == [TITLE_LINE]
    await env.poll(sources=["greenhouse"])
    assert env.pushed == [TITLE_LINE]


async def test_every_match_is_pushed_best_first_with_no_cap(env: Env, gh: GreenhouseMock) -> None:
    await baseline(env, gh, 101)
    gh.descriptions[104] = "Python platform. 8+ years of experience required."
    gh.respond(
        gh_job(101, published=OLD),
        gh_job(102, published=FRESH),
        gh_job(103, "Backend Engineer, Data", published=FRESH),
        gh_job(104, "Backend Engineer, Search", published=FRESH),
    )
    report = await env.poll(sources=["greenhouse"])

    assert len(report.pushes) == 3
    assert "Backend Engineer, Search" in report.pushes[-1]  # the lowest score goes last
    [push] = env.notifier.sent  # all three in one message
    assert push.body.startswith("3 new job postings were found via watchlist. Top score: Acme")
    rows = [env.posting("greenhouse", str(i)) for i in (102, 103, 104)]
    assert [row.tier for row in rows] == ["instant"] * 3
    assert all(row.notified_at is not None for row in rows)


async def test_a_too_senior_description_is_still_pushed_with_a_lower_score(
    env: Env, gh: GreenhouseMock
) -> None:
    await baseline(env, gh, 101)
    gh.descriptions[102] = "Python platform. 8+ years of experience required."
    gh.respond(gh_job(101, published=OLD), gh_job(102, published=FRESH))
    await env.poll(sources=["greenhouse"])

    row = env.posting("greenhouse", "102")
    assert row.tier == "instant"
    assert row.score is not None
    assert row.score < env.config.scoring.instant_threshold
    assert row.score_breakdown is not None
    assert row.score_breakdown["level_mismatch"] == -0.5
    assert row.years_required == 8  # for the dashboard's experience filter
    assert env.pushed == [TITLE_LINE]


@pytest.mark.parametrize("change", [{"status": "Dismissed"}, {"closed_at": NOW}])
async def test_a_failed_push_isnt_retried_once_triaged_or_closed(
    env: Env, gh: GreenhouseMock, change: dict[str, Any]
) -> None:
    await baseline(env, gh, 101)
    env.notifier.failures_left = 1
    gh.respond(gh_job(101, published=OLD), gh_job(102, published=FRESH))
    assert (await env.poll(sources=["greenhouse"])).failed_pushes == 1

    env.update_posting("greenhouse", "102", **change)
    if "closed_at" in change:  # gone from the board, or the next listing would reopen it
        gh.respond(gh_job(101, published=OLD))
    await env.poll(sources=["greenhouse"])
    assert env.notifier.sent == []


async def test_each_job_pushes_only_its_own_sources_matches(env: Env, gh: GreenhouseMock) -> None:
    await baseline(env, gh, 101)
    env.add_search()
    northwind = board_posting("9001", company="Northwind", location="Seattle, WA")
    env.board.results = [[], [northwind], [northwind]]
    await env.poll(sources=["linkedin"])
    env.notifier.failures_left = 1
    assert (await env.poll(sources=["linkedin"])).failed_pushes == 1

    gh.respond(gh_job(101, published=OLD), gh_job(102, published=FRESH))
    watchlist = await env.poll(sources=["greenhouse"])
    assert watchlist.pushes == [TITLE_LINE]  # the waiting LinkedIn match isn't in it

    boards = await env.poll(sources=["linkedin"])
    [line] = boards.pushes
    assert line.startswith("Northwind · Backend Engineer")
    assert [push.body.split(".")[0] for push in env.notifier.sent] == [
        "1 new job posting was found via watchlist",
        "1 new job posting was found via job boards",
    ]


async def test_filtered_titles_are_stored_with_a_reason(env: Env, gh: GreenhouseMock) -> None:
    await baseline(env, gh, 101)
    gh.respond(gh_job(101, published=OLD), gh_job(102, "Engineering Manager", published=FRESH))
    await env.poll(sources=["greenhouse"])
    row = env.posting("greenhouse", "102")
    assert row.tier == "filtered"
    assert row.tier_reason is not None
    assert "manager" in row.tier_reason


async def test_dry_run_sends_nothing_and_rolls_everything_back(
    env: Env, gh: GreenhouseMock
) -> None:
    await baseline(env, gh, 101)
    gh.respond(gh_job(101, published=OLD), gh_job(102, published=FRESH))
    postings_before, runs_before = len(env.postings()), len(env.runs())

    report = await env.poll(sources=["greenhouse"], dry_run=True)

    assert report.dry_run
    assert report.pushes == [TITLE_LINE]
    assert env.notifier.sent == []
    assert len(env.postings()) == postings_before
    assert len(env.runs()) == runs_before

    await env.poll(sources=["greenhouse"])
    assert env.pushed == [TITLE_LINE]


async def test_a_pending_posting_from_an_interrupted_cycle_is_finished(
    env: Env, gh: GreenhouseMock
) -> None:
    await baseline(env, gh, 101)
    interrupted = make_posting(
        external_id="102",
        url="https://job-boards.greenhouse.io/acme/jobs/102",
        canonical_url="greenhouse:acme:102",
        locations=("San Francisco, CA", "Remote (US)"),
        posted_at=FRESH,
    )
    with env.sessions() as session:
        repo.insert_if_absent(
            session,
            interrupted,
            company_id=env.company("acme").id,
            fingerprint=posting_fingerprint(interrupted),
            tier="pending",
            tier_reason=None,
            now=NOW - timedelta(minutes=10),
        )
        session.commit()

    gh.respond(gh_job(101, published=OLD), gh_job(102, published=FRESH))
    await env.poll(sources=["greenhouse"])

    assert env.posting("greenhouse", "102").tier == "instant"
    assert env.pushed == [TITLE_LINE]
