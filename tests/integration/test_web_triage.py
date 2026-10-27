"""Dashboard triage tools: years asked, when a posting was moved (and the sort by it), CSV
export, bulk dismiss with undo, and blocking a company from its card."""

import csv
import html
import io
import json
import re
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import select

from jobpulse.db.tables import BlockedCompany
from tests.factories import NOW
from tests.integration.conftest import Web, make_config, posting_ids
from tests.integration.test_web_postings import card, nav_count

# --- years of experience asked ----------------------------------------------------------------


def test_cards_show_the_years_asked_and_the_filter_reads_them(web: Web) -> None:
    junior = web.add(title="Junior", years_required=1, score=0.9)
    mid = web.add(title="Mid", years_required=4, score=0.8)
    senior = web.add(title="Senior", years_required=7, score=0.7)
    unstated = web.add(title="Unstated", years_required=None, score=0.6)

    page = web.page("/")
    assert "asks 4+ yrs" in card(page, mid)
    assert "yrs" not in card(page, unstated)
    assert web.ids(exp="0-2") == [junior]
    assert web.ids(exp="3-5") == [mid]
    assert web.ids(exp="6+") == [senior]
    assert web.ids(exp="unstated") == [unstated]
    assert web.ids(exp="lots") == [junior, mid, senior, unstated]
    assert re.search(r'<details class="more" open>', web.page("/", exp="3-5"))


# --- when a posting was moved -----------------------------------------------------------------


def test_a_status_change_records_when_and_cards_show_it(web: Web) -> None:
    posting = web.add(title="Backend Engineer")
    web.post(f"/postings/{posting}/status", {"status": "Applied", "view": "New"})
    assert web.row(posting).status_changed_at == NOW

    applied = card(web.page("/", view="applied"), posting)
    assert "applied just now" in applied
    untouched = web.add(title="Untouched")
    assert 'class="note moved"' not in card(web.page("/"), untouched)


def test_triaged_views_sort_by_when_postings_moved(web: Web) -> None:
    older = web.add(title="Older", status="Applied", status_changed_at=NOW - timedelta(days=3))
    newer = web.add(title="Newer", status="Applied", status_changed_at=NOW - timedelta(hours=2))
    unknown = web.add(title="Before the log", status="Applied", status_changed_at=None, score=0.99)

    assert web.ids(view="applied", sort="moved") == [newer, older, unknown]
    page = web.page("/", view="applied", sort="moved")
    assert re.search(r'<option value="moved"\s+selected>Recently applied</option>', page)
    assert 'value="moved"' not in web.page("/")  # nothing moved into New to sort by


# --- CSV export -------------------------------------------------------------------------------


def rows_of(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def test_csv_export_has_every_matching_posting_without_paging(web: Web) -> None:
    for n in range(55):
        web.add(title=f"Role {n}", status="Applied", score=round(0.99 - n / 100, 2))
    web.add(title="Not applied", status="Dismissed")

    response = web.client.get("/export.csv", params={"view": "applied"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == (
        'attachment; filename="jobpulse-applied-2026-10-23.csv"'
    )
    rows = rows_of(response.text)
    assert len(rows) == 55
    assert rows[0]["title"] == "Role 0"
    assert set(rows[0]) == {
        "status",
        "moved",
        "company",
        "title",
        "locations",
        "source",
        "link",
        "posted",
        "pushed",
        "first seen",
        "score",
        "pay",
        "years asked",
        "closed",
    }
    page = web.page("/", view="applied")
    assert 'href="/export.csv?view=applied"' in page


def test_csv_export_follows_the_filters_and_fills_the_columns(web: Web) -> None:
    web.add(
        title="Backend Engineer",
        company_name="Acme",
        locations=["Austin, TX", "Remote (US)"],
        status="Applied",
        status_changed_at=NOW - timedelta(hours=1),
        notified_at=NOW - timedelta(hours=5),
        compensation={"currency": "USD", "interval": "per-year-salary", "min": 1, "max": 2},
        years_required=3,
        score=0.62,
        apply_url="https://jobs.example.com/apply",
        posted_at=NOW.replace(hour=0),
        posted_at_precision="day",
    )
    web.add(title="Elsewhere", locations=["Seattle, WA"], status="Applied")

    response = web.client.get("/export.csv", params={"view": "applied", "place": "US-TX"})
    (row,) = rows_of(response.text)
    assert row["title"] == "Backend Engineer"
    assert row["status"] == "Applied"
    assert row["locations"] == "Austin, TX; Remote (US)"
    assert row["link"] == "https://jobs.example.com/apply"
    assert row["moved"] == "2026-10-23 13:00 UTC"  # in config's timezone, as on the cards
    assert row["pushed"] == "2026-10-23 09:00 UTC"
    assert row["posted"] == "2026-10-23"  # the source gave only the day
    assert row["score"] == "0.62"
    assert row["years asked"] == "3"
    assert row["pay"] == "$1–$2/yr"


def test_csv_cells_cant_become_formulas_and_secrets_stay_masked(
    make_web: Callable[..., Web],
) -> None:
    web = make_web(secrets=["supersecret-topic"])
    web.add(
        title='=HYPERLINK("https://evil.example")',
        company_name="+Acme",
        department="x",
        status="Applied",
        url="https://ntfy.sh/supersecret-topic",
        apply_url=None,
    )
    (row,) = rows_of(web.client.get("/export.csv", params={"view": "applied"}).text)
    assert row["title"] == '\'=HYPERLINK("https://evil.example")'
    assert row["company"] == "'+Acme"
    assert "supersecret-topic" not in row["link"]


# --- bulk dismiss -----------------------------------------------------------------------------


def bulk_form(page: str) -> dict[str, str]:
    form = re.search(r'<form class="bulk".*?</form>', page, re.DOTALL)
    assert form is not None, "no bulk form"
    return {
        name: html.unescape(value)
        for name, value in re.findall(r'name="([^"]+)" value="([^"]*)"', form.group(0))
    }


def test_dismiss_all_moves_what_the_filters_showed(web: Web) -> None:
    texas = [web.add(title=f"Texas {n}", locations=["Austin, TX"]) for n in range(3)]
    seattle = web.add(title="Seattle", locations=["Seattle, WA"])

    page = web.page("/", place="US-TX")
    assert "Dismiss all 3" in page
    form = bulk_form(page)
    response = web.post("/postings/bulk-status", form)

    assert response.status_code == 200
    assert "Dismissed 3 postings" in response.text
    assert [web.row(posting).status for posting in texas] == ["Dismissed"] * 3
    assert web.row(texas[0]).status_changed_at == NOW
    assert web.row(seattle).status == "New"
    assert re.search(r'id="count-New" hx-swap-oob="true">1<', response.text)

    undo = bulk_form(response.text.replace('class="undo bulk"', 'class="bulk"'))
    assert undo["status"] == "New"
    web.post("/postings/bulk-status", undo)
    assert [web.row(posting).status for posting in texas] == ["New"] * 3


def test_dismiss_all_spares_postings_that_arrived_after_the_page_was_shown(web: Web) -> None:
    shown = web.add(title="Shown", notified_at=NOW - timedelta(minutes=5))
    form = bulk_form(web.page("/"))
    later = web.add(
        title="Pushed after you looked",
        first_seen_at=NOW - timedelta(hours=1),
        notified_at=NOW + timedelta(seconds=30),
    )
    moved_back = web.add(
        title="Moved back to New after you looked",
        notified_at=NOW - timedelta(hours=3),
        status_changed_at=NOW + timedelta(seconds=40),
    )
    web.post("/postings/bulk-status", form)
    assert web.row(shown).status == "Dismissed"
    assert web.row(later).status == "New"
    assert web.row(moved_back).status == "New"


def test_dismiss_all_is_offered_only_in_new_and_saved(web: Web) -> None:
    web.add(status="Dismissed")
    web.add(status="Applied")
    assert '<form class="bulk"' not in web.page("/", view="dismissed")
    assert '<form class="bulk"' not in web.page("/", view="applied")  # your applications
    assert '<form class="bulk"' not in web.page("/")  # New is empty
    saved = web.add(status="Saved")
    form = bulk_form(web.page("/", view="saved"))
    applied_filters = {**form, "filters": "/?view=applied"}
    assert web.post("/postings/bulk-status", applied_filters).status_code == 400
    web.post("/postings/bulk-status", form)
    assert web.row(saved).status == "Dismissed"


def test_bulk_moves_need_the_csrf_token_and_sane_input(web: Web) -> None:
    posting = web.add()
    form = bulk_form(web.page("/"))
    without_token = {name: value for name, value in form.items() if name != "csrf_token"}
    refused = web.client.post(
        "/postings/bulk-status", data=without_token, headers={"HX-Request": "true"}
    )
    assert refused.status_code == 403
    assert web.row(posting).status == "New"
    assert web.post("/postings/bulk-status", {**form, "as_of": "yesterday"}).status_code == 400
    assert web.post("/postings/bulk-status", {**form, "status": "Archived"}).status_code == 400
    assert web.post("/postings/bulk-status", {"status": "New", "ids": "1,x"}).status_code == 400


def test_a_plain_form_bulk_dismiss_redirects_back(web: Web) -> None:
    web.add()
    form = bulk_form(web.page("/"))
    response = web.post("/postings/bulk-status", form, htmx=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


# --- blocking a company from its card ---------------------------------------------------------


def test_blocking_a_company_hides_it_except_where_you_applied(web: Web) -> None:
    first = web.add(title="Agency job", company_name="Jobot, Inc.")
    second = web.add(title="Agency job 2", company_name="JOBOT")
    applied = web.add(title="Agency applied", company_name="Jobot", status="Applied")
    acme = web.add(title="Backend Engineer", company_name="Acme")

    page = web.page("/")
    assert 'name="company" value="Jobot, Inc."' in card(page, first)
    response = web.post(
        "/companies/block", {"company": "Jobot, Inc.", "posting_id": str(first), "view": "New"}
    )
    assert response.status_code == 200
    notice = re.sub(r"<[^>]+>", "", html.unescape(response.text))
    assert "Blocked Jobot, Inc. · 2 postings hidden" in notice
    # the company's other cards on the page go too
    key = re.search(r'data-company="([0-9a-f]+)"', card(page, second))
    assert key is not None
    assert json.loads(response.headers["hx-trigger"]) == {
        "jobpulse:blocked": {"company": key.group(1), "keep": f"posting-{first}"}
    }

    page = web.page("/")
    assert posting_ids(page) == [acme]
    assert nav_count(page, "New") == 1
    assert web.ids(view="applied") == [applied]
    with web.sessions() as session:
        assert [b.name for b in session.scalars(select(BlockedCompany))] == ["Jobot, Inc."]

    undo = web.post("/companies/unblock", {"company": "Jobot, Inc."})
    assert undo.status_code == 200
    assert undo.headers.get("hx-refresh") == "true"
    assert set(web.ids()) == {first, second, acme}


def test_blocking_twice_or_a_nameless_posting(web: Web) -> None:
    web.add(company_name="Northwind")
    assert web.post("/companies/block", {"company": "Northwind"}).status_code == 200
    assert web.post("/companies/block", {"company": "NORTHWIND, Inc."}).status_code == 200
    with web.sessions() as session:
        assert len(list(session.scalars(select(BlockedCompany)))) == 1
    assert web.post("/companies/block", {"company": "  "}).status_code == 400


def test_a_watched_company_from_config_cant_be_blocked_here(web: Web) -> None:
    """Acme is on the test config's watchlist: blocking it there is a config error too."""
    response = web.post("/companies/block", {"company": "Acme, Inc."})
    assert response.status_code == 400
    assert "config.yaml" in html.unescape(response.text)
    with web.sessions() as session:
        assert list(session.scalars(select(BlockedCompany))) == []


def test_the_watchlist_page_lists_blocked_companies(make_web: Callable[..., Web]) -> None:
    web = make_web(config=make_config(blocklist={"companies": ["Jobot"]}))
    web.post("/companies/block", {"company": "CyberCoders"})
    page = html.unescape(web.page("/watchlist"))
    section = re.search(r'<section class="blocked".*?</section>', page, re.DOTALL)
    assert section is not None
    text = section.group(0)
    assert 'Jobot <span class="muted small">in config.yaml' in text
    assert "CyberCoders" in text
    assert text.count("Unblock") == 1  # only the dashboard's own entry

    response = web.post("/companies/unblock", {"company": "CyberCoders"}, htmx=False)
    assert response.status_code == 303
    assert "CyberCoders" not in html.unescape(web.page("/watchlist")).split("Blocked")[-1]
