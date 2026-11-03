"""The watchlist page: paste a careers URL, detect the board (URL first, then the page once),
validate it with one API call, show the result, set a priority and add it."""

import html
import json
import re
from collections.abc import Callable
from typing import Any

import pytest
import respx
from sqlalchemy import update

from jobpulse.db import repo
from jobpulse.db.tables import Company
from tests.factories import NOW
from tests.integration.conftest import Web, make_config

GH_ACMECORP = "https://boards-api.greenhouse.io/v1/boards/acmecorp/jobs"
LEVER_US = "https://api.lever.co/v0/postings/olx?mode=json"
LEVER_EU = "https://api.eu.lever.co/v0/postings/olx?mode=json"
CAREERS = "https://careers.acme.example/jobs"


def gh_board(*names: str, company: str = "Acme Corp") -> dict[str, Any]:
    return {
        "jobs": [
            {"id": 4000 + i, "title": name, "company_name": company} for i, name in enumerate(names)
        ],
        "meta": {"total": len(names)},
    }


def detect(web: Web, url: str) -> str:
    """The detection result as text (entities such as &#39; decoded)."""
    response = web.post("/watchlist/detect", {"url": url})
    assert response.status_code == 200, response.text
    return html.unescape(response.text)


def add_form(page: str) -> dict[str, str]:
    """The fields of the "add to watchlist" form in a detection result."""
    form = re.search(r'<form[^>]*action="/watchlist/add".*?</form>', page, re.DOTALL)
    assert form is not None, "no add form"
    return dict(re.findall(r'<input[^>]*name="(\w+)"[^>]*value="([^"]*)"', form.group(0)))


def has_add_form(page: str) -> bool:
    return 'action="/watchlist/add"' in page


def test_a_board_url_is_validated_with_one_call_and_offered_for_adding(
    web: Web, respx_mock: respx.MockRouter
) -> None:
    api = respx_mock.get(GH_ACMECORP).respond(json=gh_board("Backend Engineer", "SRE"))

    page = detect(web, "https://job-boards.greenhouse.io/acmecorp/jobs/4000?gh_jid=4000")

    assert api.call_count == 1
    assert "Greenhouse" in page
    assert "Acme Corp" in page
    assert "2 open jobs" in page
    fields = add_form(page)
    assert fields["ats"] == "greenhouse"
    assert fields["token"] == "acmecorp"
    assert fields["region"] == "us"
    assert fields["name"] == "Acme Corp"
    assert fields["priority"] == "0"
    assert 'name="priority"' in page


def test_a_careers_page_is_read_once_to_find_its_board(
    web: Web, respx_mock: respx.MockRouter
) -> None:
    careers = respx_mock.get(CAREERS).respond(
        html='<div id="grnhse_app"></div>'
        '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acmecorp"></script>'
    )
    api = respx_mock.get(GH_ACMECORP).respond(json=gh_board("Backend Engineer"))

    page = detect(web, CAREERS)

    assert careers.call_count == 1
    assert api.call_count == 1
    assert add_form(page)["token"] == "acmecorp"
    assert "1 open job" in page


def test_a_lever_board_that_only_answers_in_the_eu_is_added_with_region_eu(
    web: Web, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(LEVER_US).respond(404, json={"ok": False, "error": "Document not found"})
    respx_mock.get(LEVER_EU).respond(json=[{"id": "a", "text": "Backend Engineer"}])

    page = detect(web, "https://jobs.lever.co/olx")

    fields = add_form(page)
    assert (fields["ats"], fields["token"], fields["region"]) == ("lever", "olx", "eu")
    assert fields["name"] == "olx"


def test_a_board_that_does_not_exist_is_reported(web: Web, respx_mock: respx.MockRouter) -> None:
    respx_mock.get("https://boards-api.greenhouse.io/v1/boards/nosuch/jobs").respond(
        404, json={"status": 404, "error": "Job not found"}
    )
    page = detect(web, "https://boards.greenhouse.io/nosuch")
    assert "couldn't be validated" in page
    assert "HTTP 404" in page
    assert not has_add_form(page)


def test_a_page_without_a_board_is_reported(web: Web, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(CAREERS).respond(html="<h1>Careers</h1><a href='/apply'>Apply</a>")
    page = detect(web, CAREERS)
    assert "No Greenhouse, Lever or Ashby board" in page
    assert not has_add_form(page)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://localhost:8000/admin", "public"),
        ("http://192.168.1.10/careers", "public"),
        ("ftp://example.com/jobs", "public"),
        ("", "Paste"),
    ],
)
def test_urls_that_are_never_fetched(web: Web, url: str, message: str) -> None:
    # respx is active through the http fixture's transport: any request here would fail
    with respx.mock(assert_all_called=False) as router:
        page = detect(web, url)
        assert router.calls.call_count == 0
    assert message in page
    assert not has_add_form(page)


def test_an_unreadable_page_is_reported(web: Web, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(CAREERS).respond(503)
    page = detect(web, CAREERS)
    assert "couldn't read" in page.lower()


def test_a_board_already_on_the_watchlist_is_not_validated_again(web: Web) -> None:
    web.watch()  # Acme (greenhouse/acme) and Beta
    with respx.mock(assert_all_called=False) as router:
        page = detect(web, "https://job-boards.greenhouse.io/acme")
        assert router.calls.call_count == 0
    assert "already on your watchlist" in page
    assert not has_add_form(page)


def test_blocked_boards_and_companies_are_not_offered(
    make_web: Callable[..., Web], respx_mock: respx.MockRouter
) -> None:
    web = make_web(
        config=make_config(blocklist={"boards": ["greenhouse/badco"], "companies": ["Initech"]})
    )
    page = detect(web, "https://boards.greenhouse.io/badco")
    assert "blocklist" in page
    assert not has_add_form(page)

    respx_mock.get(GH_ACMECORP).respond(json=gh_board("Backend Engineer", company="Initech LLC"))
    page = detect(web, "https://boards.greenhouse.io/acmecorp")
    assert "blocklist" in page
    assert not has_add_form(page)


def test_adding_a_board_puts_it_on_the_watchlist_with_its_priority(web: Web) -> None:
    url = "https://job-boards.greenhouse.io/acmecorp"
    response = web.post(
        "/watchlist/add",
        {
            "ats": "greenhouse",
            "token": "acmecorp",
            "region": "us",
            "name": "Acme Corp",
            "priority": "4",
            "url": url,
        },
    )
    assert response.status_code == 200
    assert "Added Acme Corp" in html.unescape(response.text)

    [company] = web.companies()
    assert (company.ats_type, company.token, company.name) == (
        "greenhouse",
        "acmecorp",
        "Acme Corp",
    )
    assert company.priority == 4
    assert company.source_of_discovery == "dashboard"
    assert company.active is True
    assert company.baseline_done_at is None  # its first poll is a silent baseline
    assert company.discovered_from == url
    with web.sessions() as session:
        [target] = repo.company_targets(session)
    assert (target.token, target.priority, target.baseline_done) == ("acmecorp", 4, False)
    assert "Acme Corp" in web.page("/watchlist")

    again = web.post(
        "/watchlist/add",
        {"ats": "greenhouse", "token": "acmecorp", "region": "us", "name": "Acme", "priority": "1"},
    )
    assert "already on your watchlist" in html.unescape(again.text)
    assert len(web.companies()) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"ats": "workday", "token": "acme", "region": "us", "name": "Acme", "priority": "1"},
        {"ats": "greenhouse", "token": "bad token!", "region": "us", "name": "A", "priority": "1"},
        {"ats": "greenhouse", "token": "acme2", "region": "mars", "name": "A", "priority": "1"},
        {"ats": "greenhouse", "token": "acme2", "region": "us", "name": " ", "priority": "1"},
        {"ats": "greenhouse", "token": "acme2", "region": "us", "name": "A", "priority": "-1"},
        {"ats": "greenhouse", "token": "acme2", "region": "us", "name": "A", "priority": "lots"},
    ],
)
def test_bad_additions_are_refused(web: Web, fields: dict[str, str]) -> None:
    response = web.post("/watchlist/add", fields)
    assert response.status_code == 400
    assert web.companies() == []


def test_a_blocked_board_cannot_be_added_directly(make_web: Callable[..., Web]) -> None:
    web = make_web(config=make_config(blocklist={"boards": ["greenhouse/badco"]}))
    response = web.post(
        "/watchlist/add",
        {"ats": "greenhouse", "token": "badco", "region": "us", "name": "Bad", "priority": "0"},
    )
    assert response.status_code == 400
    assert "blocklist" in html.unescape(response.text)
    assert web.companies() == []


def add_company(web: Web, **fields: Any) -> int:
    with web.sessions() as session:
        values: dict[str, Any] = {
            "name": "Delta",
            "ats_type": "lever",
            "token": "delta",
            "source_of_discovery": "auto",
            "created_at": NOW,
            "updated_at": NOW,
            **fields,
        }
        company = Company(**values)
        session.add(company)
        session.commit()
        return company.id


def test_priority_and_pause_for_companies_not_in_config(web: Web) -> None:
    delta = add_company(web)

    response = web.post(f"/watchlist/{delta}/priority", {"priority": "7"})
    assert response.status_code == 200
    assert web.companies()[0].priority == 7

    response = web.post(f"/watchlist/{delta}/active", {"active": "0"})
    assert response.status_code == 200
    assert web.companies()[0].active is False
    with web.sessions() as session:
        assert repo.company_targets(session) == []

    web.post(f"/watchlist/{delta}/active", {"active": "1"})
    assert web.companies()[0].active is True


def test_imported_companies_are_edited_here_too(web: Web) -> None:
    """From M6 the dashboard owns every company, config.yaml's former watchlist included."""
    web.watch()
    with web.sessions() as session:
        session.execute(update(Company).values(source_of_discovery="config"))  # pre-import rows
        session.commit()
    acme = next(c for c in web.companies() if c.token == "acme")

    assert web.post(f"/watchlist/{acme.id}/priority", {"priority": "9"}).status_code == 200
    assert web.post(f"/watchlist/{acme.id}/active", {"active": "0"}).status_code == 200
    acme_after = next(c for c in web.companies() if c.token == "acme")
    assert (acme_after.priority, acme_after.active) == (9, False)

    assert web.post("/watchlist/999999/priority", {"priority": "1"}).status_code == 404


def test_the_watchlist_page_lists_every_company(web: Web) -> None:
    web.watch()
    add_company(web, discovered_from="https://jobs.lever.co/delta/abc/apply")

    page = web.page("/watchlist")

    for text in ("Acme", "Beta", "Delta", "dashboard", "auto", "baseline pending"):
        assert text in page
    assert 'href="https://job-boards.greenhouse.io/acme"' in page
    assert 'href="https://jobs.lever.co/delta"' in page
    assert 'href="https://jobs.lever.co/delta/abc/apply"' in page


# --- Workday (M4) -------------------------------------------------------------------------

WD_JOBS = "https://omega.wd5.myworkdayjobs.com/wday/cxs/omega/External/jobs"
WD_JOB_URL = (
    "https://omega.wd5.myworkdayjobs.com/en-US/External/job/Seattle-WA/"
    "Software-Engineer---New-Grad_R0012345"
)


def wd_page(total: int) -> dict[str, Any]:
    item = {
        "title": "Software Engineer - New Grad",
        "externalPath": "/job/Seattle-WA/Software-Engineer---New-Grad_R0012345",
        "locationsText": "US, WA, Seattle",
        "postedOn": "Posted Today",
        "bulletFields": ["R0012345"],
    }
    return {"total": total, "jobPostings": [item]}


def test_a_workday_board_is_validated_with_one_call_and_offered_with_keywords(
    web: Web, respx_mock: respx.MockRouter
) -> None:
    api = respx_mock.post(WD_JOBS).respond(json=wd_page(312))

    page = detect(web, WD_JOB_URL)

    assert api.call_count == 1
    body = json.loads(api.calls.last.request.content)
    assert (body["limit"], body["offset"], body["searchText"]) == (20, 0, "")
    assert "Workday" in page
    assert "312 open jobs" in page
    fields = add_form(page)
    assert (fields["ats"], fields["token"], fields["region"]) == (
        "workday",
        "omega.wd5/External",
        "us",
    )
    assert fields["name"] == "omega"  # the tenant; you can rename it before adding
    assert fields["keywords"] == ""
    assert "keyword" in page.lower()


def test_a_big_workday_board_gets_a_warning_about_keywords(
    web: Web, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(WD_JOBS).respond(json=wd_page(1400))
    page = detect(web, "https://omega.wd5.myworkdayjobs.com/External")
    assert has_add_form(page)
    assert "1400 open jobs" in page
    assert "workday.max_pages" in page


def test_an_unknown_workday_site_is_reported(web: Web, respx_mock: respx.MockRouter) -> None:
    respx_mock.post(WD_JOBS).respond(
        404, json={"errorCode": "S21", "message": "not found: Job_Posting_Site_ID=External"}
    )
    page = detect(web, "https://omega.wd5.myworkdayjobs.com/External")
    assert "couldn't be validated" in page
    assert "HTTP 404" in page
    assert not has_add_form(page)


def test_a_careers_page_that_links_a_workday_board(web: Web, respx_mock: respx.MockRouter) -> None:
    careers = respx_mock.get(CAREERS).respond(
        html='<a href="https://omega.wd5.myworkdayjobs.com/en-US/External">Open roles</a>'
    )
    api = respx_mock.post(WD_JOBS).respond(json=wd_page(12))
    page = detect(web, CAREERS)
    assert (careers.call_count, api.call_count) == (1, 1)
    assert add_form(page)["token"] == "omega.wd5/External"


def test_a_watched_workday_board_is_not_validated_again(make_web: Callable[..., Web]) -> None:
    web = make_web()
    web.watch({"name": "Omega", "ats_type": "workday", "token": "omega.wd5/External"})
    with respx.mock(assert_all_called=False) as router:
        page = detect(web, WD_JOB_URL)
        assert router.calls.call_count == 0
    assert "already on your watchlist" in page


def test_adding_a_workday_board_keeps_its_keywords(web: Web) -> None:
    response = web.post(
        "/watchlist/add",
        {
            "ats": "workday",
            "token": "omega.wd5/External",
            "region": "us",
            "name": "Omega",
            "priority": "2",
            "keywords": " backend, Platform Engineer ,backend,, ",
            "url": WD_JOB_URL,
        },
    )
    assert response.status_code == 200
    assert "Added Omega" in html.unescape(response.text)
    [company] = web.companies()
    assert (company.ats_type, company.token, company.priority) == (
        "workday",
        "omega.wd5/External",
        2,
    )
    assert company.params == {"keywords": ["backend", "Platform Engineer"]}
    assert company.source_of_discovery == "dashboard"
    with web.sessions() as session:
        [target] = repo.company_targets(session)
    assert target.keywords == ("backend", "Platform Engineer")


def test_keywords_mean_nothing_for_other_boards(web: Web) -> None:
    web.post(
        "/watchlist/add",
        {
            "ats": "greenhouse",
            "token": "acmecorp",
            "name": "Acme",
            "priority": "0",
            "keywords": "x",
        },
    )
    [company] = web.companies()
    assert not company.params
