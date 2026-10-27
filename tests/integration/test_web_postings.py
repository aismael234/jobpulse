"""Dashboard routes: the status views, one-click status changes, filters, sort order, paging,
and blocked companies."""

import html
import re
from collections.abc import Callable
from datetime import timedelta

from tests.factories import NOW
from tests.integration.conftest import Web, make_config, posting_ids

LEVER_PAY = {"currency": "USD", "interval": "per-year-salary", "min": 180000, "max": 230000}


def card(page: str, posting_id: int) -> str:
    """The HTML of one posting's card."""
    match = re.search(rf'<article[^>]*id="posting-{posting_id}".*?</article>', page, re.DOTALL)
    assert match is not None, f"posting {posting_id} not on the page"
    return match.group(0)


def nav_count(page: str, view: str) -> int:
    match = re.search(
        rf'data-view="{view}"[^>]*>.*?<span class="count"[^>]*>(\d+)</span>', page, re.DOTALL
    )
    assert match is not None, f"no count for {view}"
    return int(match.group(1))


def test_new_view_lists_untriaged_matches_with_their_age(web: Web) -> None:
    unpushed = web.add(title="Backend Engineer", score=0.45)
    pushed = web.add(
        title="Platform Engineer",
        tier="instant",
        score=0.8,
        notified_at=NOW - timedelta(hours=2),
        posted_at=None,
        posted_at_precision="none",
        first_seen_at=NOW - timedelta(hours=3),
    )
    closed = web.add(title="API Engineer", score=0.3, closed_at=NOW - timedelta(hours=1))
    for tier in ("baseline", "filtered", "stale", "pending"):
        web.add(title=f"{tier} row", tier=tier, score=None)
    web.add(title="Merged copy", tier="merged", merged_into_id=unpushed, source="linkedin")
    saved = web.add(title="Saved one", status="Saved")

    page = web.page("/")

    assert posting_ids(page) == [pushed, unpushed, closed]
    assert "posted 2 h ago" in card(page, unpushed)
    assert "first seen 3 h ago" in card(page, pushed)
    assert "closed" in card(page, closed)
    assert saved not in posting_ids(page)
    assert nav_count(page, "New") == 3
    assert nav_count(page, "Saved") == 1
    assert nav_count(page, "Applied") == 0


def test_cards_show_pay_score_breakdown_sources_and_a_safe_link(web: Web) -> None:
    posting = web.add(
        title="Backend Engineer",
        apply_url="https://jobs.lever.co/beta/0d3e/apply",
        compensation=LEVER_PAY,
        department="Engineering",
        score=0.62,
        score_breakdown={"title": 0.3, "freshness": 0.2, "stack": 0.12, "total": 0.62},
        also_seen_on=[
            {
                "source": "linkedin",
                "external_id": "4101234567",
                "url": "https://www.linkedin.com/jobs/view/4101234567",
                "matched_by": "fingerprint",
            }
        ],
    )

    html = card(web.page("/"), posting)

    assert "$180k–$230k/yr" in html
    assert "Engineering" in html
    assert "0.62" in html
    assert "title +0.30" in html
    assert 'href="https://www.linkedin.com/jobs/view/4101234567"' in html
    link = re.search(r'<a[^>]*class="title"[^>]*>', html)
    assert link is not None
    assert 'href="https://jobs.lever.co/beta/0d3e/apply"' in link.group(0)
    assert 'target="_blank"' in link.group(0)
    assert 'rel="noopener noreferrer"' in link.group(0)


def test_one_click_status_changes_move_postings_between_views(web: Web) -> None:
    posting = web.add(title="Backend Engineer")

    moved = web.post(f"/postings/{posting}/status", {"status": "Saved", "view": "New"})
    assert moved.status_code == 200
    assert "Moved to Saved" in moved.text
    assert web.row(posting).status == "Saved"
    assert web.ids() == []
    assert web.ids(view="saved") == [posting]

    for status, view in (("Applied", "Saved"), ("Dismissed", "Applied"), ("New", "Dismissed")):
        response = web.post(f"/postings/{posting}/status", {"status": status, "view": view})
        assert response.status_code == 200
        assert f"Moved to {status}" in response.text
        assert web.row(posting).status == status
    assert web.ids() == [posting]


def test_a_change_within_the_current_view_returns_the_updated_card(web: Web) -> None:
    posting = web.add(title="Backend Engineer", status="Saved")
    response = web.post(f"/postings/{posting}/status", {"status": "Saved", "view": "Saved"})
    assert posting_ids(response.text) == [posting]


def test_undo_puts_the_posting_back(web: Web) -> None:
    posting = web.add(title="Backend Engineer")
    moved = web.post(f"/postings/{posting}/status", {"status": "Dismissed", "view": "New"})
    undo = re.search(r'<form[^>]*class="undo".*?</form>', moved.text, re.DOTALL)
    assert undo is not None
    assert 'name="status" value="New"' in undo.group(0)

    restored = web.post(f"/postings/{posting}/status", {"status": "New", "view": "New"})
    assert posting_ids(restored.text) == [posting]
    assert web.row(posting).status == "New"


def test_plain_form_posts_redirect_back_to_a_dashboard_page(web: Web) -> None:
    posting = web.add(title="Backend Engineer")
    response = web.post(
        f"/postings/{posting}/status",
        {"status": "Saved", "view": "New", "return_to": "/?view=new&sort=fresh"},
        htmx=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/?view=new&sort=fresh"

    response = web.post(
        f"/postings/{posting}/status",
        {"status": "Applied", "return_to": "https://evil.example/"},
        htmx=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_bad_statuses_and_unknown_postings(web: Web) -> None:
    posting = web.add(title="Backend Engineer")
    assert web.post(f"/postings/{posting}/status", {"status": "Archived"}).status_code == 400
    assert web.post("/postings/999999/status", {"status": "Saved"}).status_code == 404
    assert web.row(posting).status == "New"


def test_filters_narrow_the_list(web: Web) -> None:
    acme = web.add(
        title="Backend Engineer",
        company_name="Acme",
        source="greenhouse",
        is_remote=False,
        score=0.5,
        description_text="Python services",
    )
    beta = web.add(
        title="Platform Engineer",
        company_name="Beta",
        source="lever",
        is_remote=True,
        score=0.6,
        locations=["Remote (US)"],
    )
    gamma = web.add(
        title="API Engineer",
        company_name="Gamma Labs",
        source="linkedin",
        is_remote=None,
        score=0.3,
        description_text="Go and Kubernetes",
    )
    delta = web.add(
        title="Backend Engineer, New Grad",
        company_name="Delta",
        source="simplify",
        score=0.4,
        closed_at=NOW - timedelta(hours=2),
    )

    assert web.ids() == [beta, acme, delta, gamma]
    assert web.ids(source="lever") == [beta]
    assert web.ids(company="acm") == [acme]
    assert web.ids(company="GAMMA") == [gamma]
    assert web.ids(remote="yes") == [beta]
    assert web.ids(remote="no") == [acme, delta]
    assert web.ids(min_score="0.45") == [beta, acme]
    assert web.ids(q="kubernetes") == [gamma]
    assert web.ids(q="platform") == [beta]
    assert web.ids(q="delta") == [delta]
    assert web.ids(open="1") == [beta, acme, gamma]
    assert web.ids(q="100%_") == []
    assert web.ids(source="lever", remote="no") == []


def test_filters_survive_in_the_form_and_links(web: Web) -> None:
    web.add(title="Backend Engineer", company_name="Acme")
    page = web.page("/", view="new", company="Acme", sort="fresh", q="backend")
    assert 'name="company" value="Acme"' in page
    assert 'name="q" value="backend"' in page
    assert re.search(r'<option value="fresh"\s+selected', page)


def test_sort_by_score_or_freshness(web: Web) -> None:
    old_best = web.add(title="Old but best", score=0.9, posted_at=NOW - timedelta(days=3))
    recent = web.add(title="Recent", score=0.5, posted_at=NOW - timedelta(hours=1))
    unscored = web.add(
        title="Unscored",
        score=None,
        posted_at=None,
        posted_at_precision="none",
        first_seen_at=NOW - timedelta(minutes=30),
    )

    assert web.ids() == [old_best, recent, unscored]
    assert web.ids(sort="score") == [old_best, recent, unscored]
    assert web.ids(sort="fresh") == [unscored, recent, old_best]


def test_long_lists_are_paged(web: Web) -> None:
    ids = [web.add(title=f"Role {n}", score=round(0.99 - n / 100, 2)) for n in range(55)]

    first = web.page("/")
    assert posting_ids(first) == ids[:50]
    assert "page=2" in first
    second = web.page("/", page="2")
    assert posting_ids(second) == ids[50:]
    assert 'href="/" rel="prev"' in second  # page 1 is the plain list
    assert "55 postings" in first


def test_other_views_show_every_posting_with_that_status(web: Web) -> None:
    stale_but_saved = web.add(title="Saved anyway", tier="stale", score=None, status="Saved")
    applied = web.add(title="Applied", status="Applied")
    dismissed = web.add(title="Dismissed", status="Dismissed", tier="instant", score=0.7)

    assert web.ids(view="saved") == [stale_but_saved]
    assert web.ids(view="applied") == [applied]
    assert web.ids(view="dismissed") == [dismissed]


def test_blocked_companies_are_hidden_except_where_you_applied(
    make_web: Callable[..., Web],
) -> None:
    web = make_web(config=make_config(blocklist={"companies": ["Jobot"]}))
    web.add(title="Agency job", company_name="Jobot, Inc.")
    web.add(title="Agency saved", company_name="JOBOT", status="Saved")
    applied = web.add(title="Agency applied", company_name="Jobot", status="Applied")
    acme = web.add(title="Backend Engineer", company_name="Acme")

    page = web.page("/")
    assert posting_ids(page) == [acme]
    assert web.ids(view="saved") == []
    assert web.ids(view="applied") == [applied]
    assert nav_count(page, "New") == 1
    assert nav_count(page, "Saved") == 0
    assert nav_count(page, "Applied") == 1


def location_options(page: str) -> dict[str, str]:
    """The location select's options: value → text."""
    select = re.search(r'<select name="place".*?</select>', page, re.DOTALL)
    assert select is not None, "no location select"
    return {
        html.unescape(value): html.unescape(" ".join(text.split()))
        for value, text in re.findall(
            r'<option value="([^"]*)"[^>]*>(.*?)</option>', select.group(0), re.DOTALL
        )
    }


def test_location_filter_by_country_state_metro_and_city(web: Web) -> None:
    sf = web.add(title="SF role", locations=["San Francisco, CA"], score=0.9)
    sunnyvale = web.add(title="Sunnyvale role", locations=["Sunnyvale"], score=0.8)
    austin = web.add(title="Austin role", locations=["Austin, TX", "Remote (US)"], score=0.7)
    remote = web.add(title="Remote role", locations=["Remote (US)"], score=0.6)
    anywhere = web.add(title="Anywhere role", locations=["Remote (Global)"], score=0.5)
    london = web.add(title="London role", locations=["London, England, UK"], score=0.4)
    nowhere = web.add(title="Unplaced role", locations=[], score=0.3)

    assert web.ids() == [sf, sunnyvale, austin, remote, anywhere, london, nowhere]
    assert web.ids(place="US") == [sf, sunnyvale, austin, remote, anywhere]
    assert web.ids(place="GB") == [anywhere, london]
    assert web.ids(place="US-CA") == [sf, sunnyvale]
    assert web.ids(place="metro:bay-area") == [sf, sunnyvale]
    assert web.ids(place="US-TX/Austin") == [austin]
    assert web.ids(place="us-tx/austin") == [austin]
    assert web.ids(place="US-WA") == []
    assert web.ids(place="nonsense") == web.ids()  # unreadable values are ignored

    options = location_options(web.page("/"))
    assert options[""] == "Any location"
    assert options["US"] == "United States (5)"
    assert options["US-CA"] == "California (2)"
    assert options["metro:bay-area"] == "San Francisco Bay Area (2)"
    assert options["US-CA/Sunnyvale"] == "Sunnyvale, CA (1)"
    assert options["GB/London"] == "London, United Kingdom (1)"


def test_location_options_follow_the_other_filters_and_keep_the_selection(web: Web) -> None:
    web.add(title="Backend Engineer", locations=["Austin, TX"], company_name="Acme")
    web.add(title="Data Engineer", locations=["Seattle, WA"], company_name="Beta")

    options = location_options(web.page("/", company="Acme"))
    assert "US-TX" in options
    assert "US-WA" not in options

    page = web.page("/", company="Acme", place="US-WA")
    assert posting_ids(page) == []
    assert re.search(r'<option value="US-WA"\s+selected>Washington \(0\)</option>', page)
    assert "No postings match these filters" in page


def test_a_location_filter_pages_through_every_match(web: Web) -> None:
    ids = [
        web.add(title=f"Role {n}", locations=["Austin, TX"], score=round(0.99 - n / 100, 2))
        for n in range(55)
    ]
    web.add(title="Elsewhere", locations=["Seattle, WA"], score=0.995)

    first = web.page("/", place="US-TX")
    assert posting_ids(first) == ids[:50]
    assert "55 postings" in first
    assert "place=US-TX&amp;page=2" in first
    assert posting_ids(web.page("/", place="US-TX", page="2")) == ids[50:]


def test_pushed_filter_reads_the_push_time_or_else_first_seen(web: Web) -> None:
    just_pushed = web.add(
        title="Pushed 30 min ago",
        first_seen_at=NOW - timedelta(hours=2),
        notified_at=NOW - timedelta(minutes=30),
        score=0.9,
    )
    never_pushed = web.add(
        title="Seen 5 h ago, never pushed", first_seen_at=NOW - timedelta(hours=5), score=0.8
    )
    # adopted from an earlier copy: pushed before this row was first seen
    adopted = web.add(
        title="Pushed 2 days ago",
        first_seen_at=NOW - timedelta(minutes=10),
        notified_at=NOW - timedelta(days=2),
        score=0.7,
    )
    old = web.add(
        title="Pushed 10 days ago",
        first_seen_at=NOW - timedelta(days=10),
        notified_at=NOW - timedelta(days=10),
        score=0.6,
    )

    assert web.ids(pushed="1h") == [just_pushed]
    assert web.ids(pushed="6h") == [just_pushed, never_pushed]
    assert web.ids(pushed="3d") == [just_pushed, never_pushed, adopted]
    assert web.ids(pushed="over-1d") == [adopted, old]
    assert web.ids(pushed="over-7d") == [old]
    assert web.ids(pushed="2h") == web.ids()  # not a preset: ignored
    page = web.page("/", pushed="6h")
    assert re.search(r'<option value="6h"\s+selected>', page)


def test_sort_by_most_recently_pushed(web: Web) -> None:
    first = web.add(title="A", score=0.9, notified_at=NOW - timedelta(hours=3))
    second = web.add(title="B", score=0.5, notified_at=NOW - timedelta(minutes=5))
    third = web.add(title="C", score=0.7, notified_at=None, first_seen_at=NOW - timedelta(hours=1))
    assert web.ids(sort="pushed") == [second, third, first]
    assert re.search(r'<option value="pushed"\s+selected>', web.page("/", sort="pushed"))


def test_pay_floor(web: Web) -> None:
    yearly = web.add(title="Yearly", compensation=LEVER_PAY, score=0.9)  # $180k–$230k
    hourly = web.add(
        title="Hourly",
        compensation={"min_amount": 50, "max_amount": 60, "currency": "USD", "interval": "hourly"},
        score=0.8,
    )  # $104k–$124.8k a year
    euros = web.add(
        title="Euros",
        compensation={"currency": "EUR", "interval": "per-year-salary", "min": 1, "max": 900000},
        score=0.7,
    )
    unlisted = web.add(title="No pay", compensation=None, score=0.6)

    assert web.ids(pay="120") == [yearly, hourly, euros, unlisted]
    assert web.ids(pay="125") == [yearly, euros, unlisted]
    assert web.ids(pay="125", pay_listed="1") == [yearly]
    assert web.ids(pay_listed="1") == [yearly, hourly]
    assert web.ids(pay="lots") == web.ids()


def more_filters_open(page: str) -> bool:
    found = re.search(r'<details class="more"[^>]*>', page)
    assert found is not None, "no More filters"
    return " open" in found.group(0)


def test_more_filters_open_only_when_one_of_them_is_set(web: Web) -> None:
    web.add()
    assert not more_filters_open(web.page("/"))
    assert not more_filters_open(web.page("/", place="US", pushed="6h", q="python"))
    for params in ({"company": "Acme"}, {"pay": "100"}, {"open": "1"}, {"remote": "yes"}):
        assert more_filters_open(web.page("/", **params)), params


def test_an_llm_second_opinion_shows_with_its_reason(web: Web) -> None:
    """M4: the LLM layer's move on the score, and its reason on hover."""
    reason = "score 0.50 < 0.55; llm -0.09: mid, backend 20%, fit 20% (Mostly frontend.)"
    web.add(
        score=0.50, score_breakdown={"title": 0.3, "llm": -0.09, "total": 0.5}, tier_reason=reason
    )
    web.add(score=0.45)
    page = html.unescape(web.page("/"))
    assert page.count("LLM -0.09") == 1
    assert f'title="{reason}"' in page
