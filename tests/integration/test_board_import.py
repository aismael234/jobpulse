"""``jobpulse companies import``: the boards behind a file's job links join the watchlist, each
checked like a pasted URL; Workday boards with keywords that fit what a poll reads."""

import json
from pathlib import Path
from typing import Any

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from jobpulse.board_import import BoardResult, ImportReport, Link, import_boards, read_links
from jobpulse.cli import app
from jobpulse.db import repo
from jobpulse.db.tables import Company
from jobpulse.httpclient import HttpClient
from jobpulse.sources.ashby import AshbyAdapter
from jobpulse.sources.ats_detect import AtsDetector
from jobpulse.sources.greenhouse import GreenhouseAdapter
from jobpulse.sources.lever import LeverAdapter
from tests.factories import NOW
from tests.integration.conftest import make_config

pytestmark = pytest.mark.respx(assert_all_called=False)

GH_MIXPANEL = "https://job-boards.greenhouse.io/mixpanel/jobs/7311234"
LEVER_JEEVES = "https://jobs.lever.co/tryjeeves/75cb18c2-fb97-4469-b000-2df1b7d73ce9"
ASHBY_LUMOS = "https://jobs.ashbyhq.com/lumos/37ecfa5a-f047-4b70-a29b-79cc885df706"
WD_POSTMAN = (
    "https://postman.wd108.myworkdayjobs.com/careers/job/San-Francisco-CA-US/"
    "Senior-Fullstack-Software-Engineer_JR1000019"
)
WD_POSTMAN_API = "https://postman.wd108.myworkdayjobs.com/wday/cxs/postman/careers/jobs"
WD_NVIDIA = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/X_JR1"
WD_NVIDIA_API = "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
GH_JID_PAGE = "https://www.8am.com/openings/?gh_jid=4724462006"
LINKEDIN = "https://www.linkedin.com/jobs/view/full-stack-engineer-at-clickhouse-4481397723"
ICIMS = "https://careers-americas.icims.com/jobs/12345/software-engineer/job"
LOGO = "https://crustdata-media.s3.us-east-2.amazonaws.com/company/abc.jpg"


def gh_board(count: int, company: str = "Mixpanel") -> dict[str, Any]:
    return {
        "jobs": [{"id": n, "title": f"Role {n}", "company_name": company} for n in range(count)]
    }


def wd_answer(
    request: Any, totals: dict[str, int], titles: dict[str, list[str]] | None = None
) -> respx.MockResponse:
    """A Workday search: the total for the request's keyword ("" is the whole board) and its
    first page of titles."""
    keyword = json.loads(request.content)["searchText"]
    page = [{"title": title} for title in (titles or {}).get(keyword, ["Customer Care Rep"])]
    return respx.MockResponse(json={"total": totals[keyword], "jobPostings": page})


def export(*entries: tuple[str, str]) -> str:
    """An export shaped like the other service's: the job's URL and company in ``job``, a logo
    beside them, and a description that mentions other links."""
    return json.dumps(
        {
            "job_applications": [
                {
                    "status": "APPLIED",
                    "job": {
                        "url": url,
                        "company_name": company,
                        "company_logo_url": LOGO,
                        "description": f"See all roles at https://boards.greenhouse.io/other {url}",
                    },
                }
                for url, company in entries
            ]
        }
    )


@pytest.fixture
def detector(http: HttpClient) -> AtsDetector:
    return AtsDetector(http)


def companies(database: sessionmaker[Session]) -> dict[str, Company]:
    with database() as session:
        return {f"{c.ats_type}/{c.token}": c for c in session.scalars(select(Company))}


def outcomes(report: ImportReport) -> dict[str, str]:
    return {f"{b.ref.ats}/{b.ref.token}": b.outcome for b in report.boards}


def by_board(report: ImportReport, board: str) -> BoardResult:
    return next(b for b in report.boards if f"{b.ref.ats}/{b.ref.token}" == board)


# --- reading links ----------------------------------------------------------------------------


def test_json_links_are_url_values_with_the_company_beside_them() -> None:
    links = read_links(
        export((GH_MIXPANEL, "Mixpanel"), (ASHBY_LUMOS, "Lumos"), (GH_MIXPANEL, "Mixpanel"))
    )
    assert links == [
        Link(GH_MIXPANEL, "Mixpanel"),
        Link(LOGO, "Mixpanel"),
        Link(ASHBY_LUMOS, "Lumos"),
    ]


def test_a_company_name_is_inherited_from_the_enclosing_object() -> None:
    text = json.dumps({"company": "Acme", "jobs": [{"url": GH_MIXPANEL}], "other": [LEVER_JEEVES]})
    assert read_links(text) == [Link(GH_MIXPANEL, "Acme"), Link(LEVER_JEEVES, "Acme")]


def test_text_files_give_every_url_in_them() -> None:
    text = f"Applied to {GH_MIXPANEL}, and {ASHBY_LUMOS}.\n<{LEVER_JEEVES}>\n{GH_MIXPANEL}"
    assert read_links(text) == [Link(GH_MIXPANEL), Link(ASHBY_LUMOS), Link(LEVER_JEEVES)]


# --- importing --------------------------------------------------------------------------------


async def test_boards_are_validated_and_added_for_a_silent_first_poll(
    database: sessionmaker[Session], detector: AtsDetector, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(GreenhouseAdapter.list_url("mixpanel")).respond(json=gh_board(120))
    respx_mock.get(LeverAdapter.list_url("tryjeeves")).respond(
        404, json={"ok": False, "error": "Document not found"}
    )
    respx_mock.get(LeverAdapter.list_url("tryjeeves", "eu")).respond(json=[{"id": "a"}])
    respx_mock.get(AshbyAdapter.list_url("lumos")).respond(json={"jobs": [{"id": "1"}] * 30})
    logo = respx_mock.get(LOGO)
    linkedin = respx_mock.get(url__startswith="https://www.linkedin.com")
    icims = respx_mock.get(url__startswith="https://careers-americas.icims.com")
    links = [
        Link(GH_MIXPANEL, "Mixpanel Inc."),
        Link(LEVER_JEEVES, "Jeeves"),
        Link(ASHBY_LUMOS, "Lumos"),
        Link(LINKEDIN, "ClickHouse"),
        Link(ICIMS, "Big Co"),
        Link(LOGO, "Lumos"),
    ]

    report = await import_boards(
        links,
        detector=detector,
        sessions=database,
        config=make_config(),
        priority=2,
        clock=lambda: NOW,
    )

    assert outcomes(report) == {
        "greenhouse/mixpanel": "added",
        "lever/tryjeeves": "added",
        "ashby/lumos": "added",
    }
    assert (report.links, report.board_links, report.job_board_links, report.other_links) == (
        6,
        3,
        1,
        2,
    )
    # other links are never fetched
    assert [route.called for route in (logo, linkedin, icims)] == [False, False, False]
    stored = companies(database)
    assert set(stored) == {"greenhouse/mixpanel", "lever/tryjeeves", "ashby/lumos"}
    assert stored["greenhouse/mixpanel"].name == "Mixpanel"  # the board's own name first
    assert stored["ashby/lumos"].name == "Lumos"  # else the name beside the link
    assert stored["lever/tryjeeves"].region == "eu"
    for company in stored.values():
        assert (company.source_of_discovery, company.priority, company.active) == (
            "import",
            2,
            True,
        )
        assert company.baseline_done_at is None
    assert stored["ashby/lumos"].discovered_from == ASHBY_LUMOS


async def test_workday_boards_keep_the_keywords_a_poll_can_read(
    database: sessionmaker[Session], detector: AtsDetector, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(WD_POSTMAN_API).mock(
        side_effect=lambda request: wd_answer(
            request, {"": 25, "software engineer": 16, "full stack": 600}
        )
    )
    respx_mock.post(WD_NVIDIA_API).mock(
        side_effect=lambda request: wd_answer(
            request, {"": 2000, "software engineer": 1750, "full stack": 501}
        )
    )
    report = await import_boards(
        [Link(WD_POSTMAN, "Postman"), Link(WD_NVIDIA, "NVIDIA")],
        detector=detector,
        sessions=database,
        config=make_config(workday={"max_pages": 25}),
        keywords=["software engineer", "full stack"],
        clock=lambda: NOW,
    )

    postman = by_board(report, "workday/postman.wd108/careers")
    assert postman.outcome == "added"
    assert postman.keywords == (("software engineer", 16),)  # one page: kept, whatever it finds
    assert postman.dropped == (("full stack", 600),)  # over 25 pages of 20
    nvidia = by_board(report, "workday/nvidia.wd5/NVIDIAExternalCareerSite")  # the site's case
    assert nvidia.outcome == "too broad"
    stored = companies(database)
    assert set(stored) == {"workday/postman.wd108/careers"}
    assert stored["workday/postman.wd108/careers"].params == {"keywords": ["software engineer"]}
    assert stored["workday/postman.wd108/careers"].name == "Postman"


async def test_a_keyword_whose_first_page_matches_no_title_rule_is_dropped(
    database: sessionmaker[Session], detector: AtsDetector, respx_mock: respx.MockRouter
) -> None:
    """Workday's search is fuzzy: "full stack" finds a bank's "Full Time" jobs."""
    respx_mock.post(WD_POSTMAN_API).mock(
        side_effect=lambda request: wd_answer(
            request,
            {"": 900, "backend": 150, "full stack": 150},
            {"backend": ["Customer Care Rep", "Senior Backend Engineer"]},
        )
    )
    report = await import_boards(
        [Link(WD_POSTMAN, "Postman")],
        detector=detector,
        sessions=database,
        config=make_config(),
        keywords=["backend", "full stack"],
    )
    postman = by_board(report, "workday/postman.wd108/careers")
    assert postman.keywords == (("backend", 150),)
    assert postman.unmatched == (("full stack", 150),)


async def test_workday_boards_need_keywords(
    database: sessionmaker[Session], detector: AtsDetector, respx_mock: respx.MockRouter
) -> None:
    api = respx_mock.post(WD_POSTMAN_API)
    report = await import_boards(
        [Link(WD_POSTMAN, "Postman")], detector=detector, sessions=database, config=make_config()
    )
    assert outcomes(report) == {"workday/postman.wd108/careers": "needs keywords"}
    assert not api.called
    assert companies(database) == {}


async def test_a_gh_jid_page_is_read_once_to_find_its_board(
    database: sessionmaker[Session], detector: AtsDetector, respx_mock: respx.MockRouter
) -> None:
    page = respx_mock.get(url__startswith="https://www.8am.com/openings/").respond(
        html='<script src="https://boards.greenhouse.io/embed/job_board/js?for=8am"></script>'
    )
    respx_mock.get(GreenhouseAdapter.list_url("8am")).respond(json=gh_board(12, "8am"))
    report = await import_boards(
        [Link(GH_JID_PAGE, "8am"), Link(GH_JID_PAGE.replace("4724462006", "1"), "8am")],
        detector=detector,
        sessions=database,
        config=make_config(),
    )
    assert page.call_count == 1  # once per host
    assert outcomes(report) == {"greenhouse/8am": "added"}


async def test_watched_blocked_invalid_and_big_boards_are_left_out(
    database: sessionmaker[Session], detector: AtsDetector, respx_mock: respx.MockRouter
) -> None:
    config = make_config(
        blocklist={"companies": ["Initech"], "boards": ["ashby/blockedco"]},
        discovery={"max_board_jobs": 100},
    )
    with database() as session:  # already on the watchlist: not validated again
        repo.add_company(
            session,
            ats="greenhouse",
            token="mixpanel",
            region="us",
            name="Mixpanel",
            source="dashboard",
            priority=0,
            discovered_from=None,
            now=NOW,
        )
        session.commit()
    respx_mock.get(GreenhouseAdapter.list_url("initech")).respond(json=gh_board(3, "Initech LLC"))
    respx_mock.get(GreenhouseAdapter.list_url("nosuch")).respond(404, json={"status": 404})
    respx_mock.get(GreenhouseAdapter.list_url("huge")).respond(json=gh_board(101, "Huge"))
    watched = respx_mock.get(GreenhouseAdapter.list_url("mixpanel"))
    blocked_board = respx_mock.get(AshbyAdapter.list_url("blockedco"))

    report = await import_boards(
        [
            Link(GH_MIXPANEL, "Mixpanel"),
            Link("https://job-boards.greenhouse.io/initech/jobs/1", None),
            Link("https://job-boards.greenhouse.io/nosuch/jobs/1", "Nosuch"),
            Link("https://job-boards.greenhouse.io/huge/jobs/1", "Huge"),
            Link("https://jobs.ashbyhq.com/blockedco/1", "Blocked Co"),
        ],
        detector=detector,
        sessions=database,
        config=config,
    )

    assert outcomes(report) == {
        "greenhouse/mixpanel": "watched",
        "greenhouse/initech": "blocked",  # the board's own name is on the blocklist
        "greenhouse/nosuch": "invalid",
        "greenhouse/huge": "too big",
        "ashby/blockedco": "blocked",
    }
    assert [route.called for route in (watched, blocked_board)] == [False, False]
    assert set(companies(database)) == {"greenhouse/mixpanel"}


async def test_a_dashboard_block_counts_too(
    database: sessionmaker[Session], detector: AtsDetector, respx_mock: respx.MockRouter
) -> None:
    with database() as session:
        repo.block_company(session, "Lumos", now=NOW)
        session.commit()
    respx_mock.get(AshbyAdapter.list_url("lumos")).respond(json={"jobs": []})
    report = await import_boards(
        [Link(ASHBY_LUMOS, "Lumos")], detector=detector, sessions=database, config=make_config()
    )
    assert outcomes(report) == {"ashby/lumos": "blocked"}


async def test_a_dry_run_adds_nothing(
    database: sessionmaker[Session], detector: AtsDetector, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(GreenhouseAdapter.list_url("mixpanel")).respond(json=gh_board(5))
    report = await import_boards(
        [Link(GH_MIXPANEL)],
        detector=detector,
        sessions=database,
        config=make_config(),
        dry_run=True,
    )
    assert outcomes(report) == {"greenhouse/mixpanel": "would add"}
    assert companies(database) == {}


# --- the command ------------------------------------------------------------------------------

runner = CliRunner()


def test_the_command_reports_each_board_and_a_summary(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    base = ["--data-dir", str(tmp_path)]  # no profile needed to import boards
    assert runner.invoke(app, [*base, "db", "upgrade"]).exit_code == 0
    respx_mock.get(GreenhouseAdapter.list_url("mixpanel")).respond(json=gh_board(120))
    respx_mock.post(WD_POSTMAN_API).mock(
        side_effect=lambda request: wd_answer(request, {"": 25, "software engineer": 16})
    )
    file = tmp_path / "export.json"
    file.write_text(export((GH_MIXPANEL, "Mixpanel"), (WD_POSTMAN, "Postman"), (LINKEDIN, "X")))

    dry = runner.invoke(
        app,
        [
            *base,
            "companies",
            "import",
            str(file),
            "--dry-run",
            "--workday-keywords",
            "software engineer",
        ],
    )
    assert dry.exit_code == 0, dry.output
    assert "would add greenhouse/mixpanel" in dry.output

    result = runner.invoke(
        app, [*base, "companies", "import", str(file), "--workday-keywords", "software engineer"]
    )
    assert result.exit_code == 0, result.output
    assert "added greenhouse/mixpanel: Mixpanel, 120 open jobs" in result.output
    assert "added workday/postman.wd108/careers: Postman, searching software engineer (16)" in (
        result.output
    )
    assert "1 job-board link" in result.output
    assert "Added 2 boards" in result.output

    again = runner.invoke(
        app, [*base, "companies", "import", str(file), "--workday-keywords", "software engineer"]
    )
    assert "already on the watchlist: greenhouse/mixpanel" in again.output
    listed = runner.invoke(app, [*base, "companies", "list"])
    assert "(import)" in listed.output


@pytest.mark.parametrize(
    "extra",
    [["--workday-keywords", "x" * 200], ["--priority", "-1"], ["--priority", "1001"]],
)
def test_bad_options_are_refused(tmp_path: Path, extra: list[str]) -> None:
    file = tmp_path / "links.txt"
    file.write_text(GH_MIXPANEL)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")  # no profile needed to import boards
    base = ["--config", str(config_path), "--env-file", str(tmp_path / ".env")]
    result = runner.invoke(app, [*base, "companies", "import", str(file), *extra])
    assert result.exit_code == 2
