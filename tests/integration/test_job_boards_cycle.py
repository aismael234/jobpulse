"""Indeed and LinkedIn through the real JobSpy adapter (over a scripted ``scrape_jobs``):
searches from config, items 5, 6 and 8, and the LinkedIn back-off."""

from datetime import timedelta

import pytest
import respx

from tests.factories import INDEED_KEY, LINKEDIN_ID, NOW, jobspy_frame, jobspy_row
from tests.integration.conftest import (
    LEVER_LIST,
    Env,
    GreenhouseMock,
    ScriptedScrape,
    gh_job,
    lever_posting,
)

pytestmark = pytest.mark.respx(assert_all_called=False)

OLD = NOW - timedelta(days=3)
BLOCKED = "response status code 429 (blocked: use a proxy)"
LEVER_ID = "5c6e2a0e-1f2b-4c3d-9e8f-0a1b2c3d4e5f"


def linkedin(
    external_id: str, title: str = "Backend Engineer", **fields: object
) -> dict[str, object]:
    return jobspy_row("linkedin", external_id=external_id, title=title, **fields)


def indeed(key: str, title: str = "Backend Engineer", **fields: object) -> dict[str, object]:
    return jobspy_row("indeed", external_id=key, title=title, **fields)


def both_searches(env: Env) -> None:
    env.add_search("backend-sf", "linkedin", location="San Francisco, CA")
    env.add_search("backend-remote", "indeed", location="United States", is_remote=True)


async def test_searches_come_from_config_and_each_has_its_own_baseline(
    env: Env, boards: ScriptedScrape
) -> None:
    both_searches(env)
    boards.queue("linkedin", jobspy_frame([linkedin("4101000001")]))
    boards.queue("indeed", jobspy_frame([indeed("a1b2c3d4e5f60001")]))
    report = await env.poll(sources=["linkedin", "indeed"])

    assert env.notifier.sent == []
    assert {row.tier for row in env.postings()} == {"baseline"}
    assert {(t.source, t.key, t.is_baseline) for t in report.targets} == {
        ("linkedin", "backend-sf", True),
        ("indeed", "backend-remote", True),
    }
    assert env.search("backend-sf").baseline_done_at == NOW
    assert env.search("backend-remote").params["is_remote"] is True
    by_site = {call["site_name"][0]: call for call in boards.calls}
    assert by_site["linkedin"]["hours_old"] == 3
    assert by_site["linkedin"]["location"] == "San Francisco, CA"
    assert by_site["indeed"]["is_remote"] is True
    assert all("proxies" not in call for call in boards.calls)
    assert all(call["site_name"] in (["linkedin"], ["indeed"]) for call in boards.calls)


async def test_overlapping_windows_create_no_duplicates_or_second_alerts(
    env: Env, boards: ScriptedScrape
) -> None:
    """Item 5: the same LinkedIn id and Indeed jk in consecutive overlapping runs."""
    both_searches(env)
    boards.queue(
        "linkedin",
        jobspy_frame([linkedin("4101000001")]),
        jobspy_frame([linkedin("4101000001"), linkedin("4101000002", "Backend Engineer, Data")]),
        jobspy_frame([linkedin("4101000002", "Backend Engineer, Data")]),
    )
    boards.queue(
        "indeed",
        jobspy_frame([indeed("a1b2c3d4e5f60001")]),
        jobspy_frame(
            [indeed("a1b2c3d4e5f60001"), indeed("a1b2c3d4e5f60002", "Backend Engineer, Search")]
        ),
        jobspy_frame([indeed("a1b2c3d4e5f60002", "Backend Engineer, Search")]),
    )
    for _ in range(3):
        await env.poll(sources=["linkedin", "indeed"])

    assert sorted((row.source, row.external_id) for row in env.postings()) == [
        ("indeed", "a1b2c3d4e5f60001"),
        ("indeed", "a1b2c3d4e5f60002"),
        ("linkedin", "4101000001"),
        ("linkedin", "4101000002"),
    ]
    assert len(env.notifier.sent) == 2


async def test_a_linkedin_429_fails_only_the_linkedin_run(env: Env, boards: ScriptedScrape) -> None:
    """Item 8: attrs["errors"]["linkedin"] with a 429 is a failed LinkedIn run (http_429),
    even though Indeed rows came back in the same cycle."""
    both_searches(env)
    boards.queue(
        "linkedin",
        jobspy_frame([]),
        jobspy_frame([linkedin("4101000003")], errors={"linkedin": BLOCKED}),
    )
    boards.queue("indeed", jobspy_frame([]), jobspy_frame([indeed("a1b2c3d4e5f60003")]))
    await env.poll(sources=["linkedin", "indeed"])
    report = await env.poll(sources=["linkedin", "indeed"])

    by_key = {target.key: target for target in report.targets}
    assert not by_key["backend-sf"].ok
    assert by_key["backend-sf"].error_kind == "http_429"
    assert by_key["backend-remote"].ok
    runs = {(run.source, run.ok, run.error_kind) for run in env.runs()[2:]}
    assert runs == {("linkedin", False, "http_429"), ("indeed", True, None)}
    assert [row.external_id for row in env.postings() if row.source == "linkedin"] == []
    assert len(env.notifier.sent) == 1  # the Indeed job


async def test_after_a_429_the_other_linkedin_searches_wait(
    env: Env, boards: ScriptedScrape
) -> None:
    env.add_search("backend-sf", "linkedin")
    env.add_search("platform-sf", "linkedin", search_term="platform engineer")
    boards.queue("linkedin", jobspy_frame([]), jobspy_frame([]))
    await env.poll(sources=["linkedin"])
    boards.queue("linkedin", jobspy_frame([], errors={"linkedin": BLOCKED}))
    report = await env.poll(sources=["linkedin"])

    assert boards.sites() == ["linkedin"] * 3  # the second search wasn't attempted
    skipped = next(target for target in report.targets if target.key == "platform-sf")
    assert skipped.skipped
    assert skipped.run_id is None
    assert len(env.runs()) == 3  # two baselines and the 429; nothing for the skipped search


@pytest.mark.parametrize(
    ("direct_url", "primary"),
    [
        (
            "https://boards.greenhouse.io/acme/jobs/101?gh_src=abc123&utm_source=Indeed",
            ("greenhouse", "101"),
        ),
        ("https://careers.acme.example/jobs?gh_jid=101&gh_src=in", ("greenhouse", "101")),
        (f"https://jobs.lever.co/beta/{LEVER_ID}/apply?lever-source=Indeed", ("lever", LEVER_ID)),
    ],
    ids=["greenhouse-tracking-params", "custom-domain-gh_jid", "lever-apply-url"],
)
async def test_a_job_board_copy_of_an_ats_posting_is_merged_not_pushed(
    env: Env,
    gh: GreenhouseMock,
    respx_mock: respx.MockRouter,
    boards: ScriptedScrape,
    direct_url: str,
    primary: tuple[str, str],
) -> None:
    """Item 6, through the real adapter: the direct URL's canonical form finds the ATS row."""
    gh.respond(gh_job(101, published=OLD, url="https://careers.acme.example/jobs?gh_jid=101"))
    respx_mock.get(LEVER_LIST).respond(200, json=[lever_posting(LEVER_ID, created=OLD)])
    await env.poll(sources=["greenhouse", "lever"])
    env.add_search("backend-remote", "indeed")
    boards.queue(
        "indeed",
        jobspy_frame([]),
        jobspy_frame([indeed(INDEED_KEY, "Backend Engineer (Indeed)", job_url_direct=direct_url)]),
    )
    await env.poll(sources=["indeed"])
    await env.poll(sources=["indeed"])

    assert env.notifier.sent == []
    duplicate = env.posting("indeed", INDEED_KEY)
    original = env.posting(*primary)
    assert duplicate.merged_into_id == original.id
    # The custom domain is known from the stored Greenhouse URLs, so all three are ATS keys.
    assert [(e["source"], e["external_id"], e["matched_by"]) for e in original.also_seen_on] == [
        ("indeed", INDEED_KEY, "canonical url")
    ]


async def test_a_linkedin_copy_without_an_ats_url_merges_by_fingerprint(
    env: Env, gh: GreenhouseMock, boards: ScriptedScrape
) -> None:
    gh.respond(gh_job(101, published=OLD, location="San Francisco, CA"))
    await env.poll(sources=["greenhouse"])
    env.add_search()
    boards.queue(
        "linkedin",
        jobspy_frame([]),
        jobspy_frame(
            [linkedin(LINKEDIN_ID, company="Acme, Inc.", location="San Francisco, CA, USA")]
        ),
    )
    await env.poll(sources=["linkedin"])
    await env.poll(sources=["linkedin"])

    assert env.notifier.sent == []
    original = env.posting("greenhouse", "101")
    assert env.posting("linkedin", LINKEDIN_ID).merged_into_id == original.id
    assert original.also_seen_on[0]["matched_by"] == "fingerprint"


async def test_a_changed_search_starts_a_new_baseline(env: Env, boards: ScriptedScrape) -> None:
    env.add_search()
    boards.queue("linkedin", jobspy_frame([]))
    await env.poll(sources=["linkedin"])

    env.reconfigure(searches=[])
    env.add_search(search_term="platform engineer")
    boards.queue(
        "linkedin",
        jobspy_frame([linkedin("4101000004")]),
        jobspy_frame([linkedin("4101000004"), linkedin("4101000005", "Backend Engineer II")]),
    )
    report = await env.poll(sources=["linkedin"])
    assert report.targets[0].is_baseline
    assert env.notifier.sent == []
    assert env.search().search_term == "platform engineer"

    await env.poll(sources=["linkedin"])
    assert len(env.notifier.sent) == 1


async def test_a_search_removed_from_config_is_deactivated(
    env: Env, boards: ScriptedScrape
) -> None:
    env.add_search()
    boards.queue("linkedin", jobspy_frame([]))
    await env.poll(sources=["linkedin"])
    env.reconfigure(searches=[])
    report = await env.poll(sources=["linkedin"])
    assert report.targets == []
    assert env.search().active is False
    assert len(boards.calls) == 1
