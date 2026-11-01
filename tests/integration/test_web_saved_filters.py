"""M5.1 on the dashboard: Settings creates, edits, switches and deletes up to 8 labeled filters
(one per color), says which push mode is on, flags a filter that no longer reads back, and has
a color-blind mode. The postings page gets a button per saved filter with a badge of new
matches since it was last opened, and bullets on the cards that fit."""

import html
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from jobpulse.db import saved_filters
from jobpulse.db.query_params import filter_params, parse_query
from jobpulse.db.tables import SavedFilter
from tests.factories import NOW
from tests.integration.conftest import Web
from tests.integration.test_web_postings import card


def saved(web: Web) -> list[SavedFilter]:
    with web.sessions() as session:
        return list(session.scalars(select(SavedFilter).order_by(SavedFilter.id)))


def add_filter(
    web: Web, label: str, slot: int, *, now: Any = NOW - timedelta(hours=2), **params: str
) -> int:
    with web.sessions() as session:
        row = saved_filters.create(
            session, label=label, slot=slot, params=filter_params(parse_query(params)), now=now
        )
        session.commit()
        return row.id


def form(label: str = "Austin contract", slot: str = "0", **params: str) -> dict[str, str]:
    return {"label": label, "slot": slot, **params}


# --- Settings ---------------------------------------------------------------------------------


def test_settings_says_which_push_mode_is_on(web: Web) -> None:
    page = html.unescape(web.page("/settings"))
    assert "No saved filters: every match is pushed, one message per cycle." in page

    first = add_filter(web, "Austin", 0, place="US-TX")
    page = html.unescape(web.page("/settings"))
    assert "Only matches that fit an active saved filter are pushed, one message each." in page

    with web.sessions() as session:
        saved_filters.set_active(session, first, False, now=NOW)
        session.commit()
    page = html.unescape(web.page("/settings"))
    assert "Every saved filter is off: nothing is pushed." in page


def test_creating_a_filter_keeps_every_field_written_out(web: Web) -> None:
    response = web.post(
        "/settings/filters", form(place="US-TX", contract="only", q=" python "), htmx=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    [row] = saved(web)
    assert row.label == "Austin contract"
    assert row.slot == 0
    assert row.active
    assert row.fires_from == NOW
    assert row.params == {
        **filter_params(parse_query({})),
        "place": "US-TX",
        "contract": "only",
        "q": "python",
    }


def test_saving_needs_the_form_token(web: Web) -> None:
    response = web.client.post("/settings/filters", data=form(), follow_redirects=False)
    assert response.status_code == 403
    assert saved(web) == []


def test_a_label_and_a_free_color_are_required_and_eight_is_the_most(web: Web) -> None:
    assert web.post("/settings/filters", form(label="  "), htmx=False).status_code == 400
    add_filter(web, "Austin", 0)
    taken = web.post("/settings/filters", form(label="Other", slot="0"), htmx=False)
    assert taken.status_code == 400
    assert "color is taken" in html.unescape(taken.text)
    assert web.post("/settings/filters", form(slot="9"), htmx=False).status_code == 400
    for slot in range(1, 8):
        add_filter(web, f"F{slot}", slot)
    full = web.post("/settings/filters", form(label="Ninth", slot="3"), htmx=False)
    assert full.status_code == 400
    assert "8 saved filters" in html.unescape(full.text)
    assert len(saved(web)) == 8


def test_editing_moves_fires_from_only_when_the_filters_change(web: Web) -> None:
    filter_id = add_filter(web, "Austin", 0, place="US-TX")
    before = saved(web)[0].fires_from

    web.post(f"/settings/filters/{filter_id}", form("Texas", "2", place="US-TX"), htmx=False)
    [row] = saved(web)
    assert (row.label, row.slot, row.fires_from) == ("Texas", 2, before)

    web.post(f"/settings/filters/{filter_id}", form("Texas", "2", place="US-CA"), htmx=False)
    [row] = saved(web)
    assert row.params["place"] == "US-CA"
    assert row.fires_from == NOW


def test_switching_off_and_on_and_deleting(web: Web) -> None:
    filter_id = add_filter(web, "Austin", 0)
    web.post(f"/settings/filters/{filter_id}/active", {"active": "0"}, htmx=False)
    assert not saved(web)[0].active
    web.post(f"/settings/filters/{filter_id}/active", {"active": "1"}, htmx=False)
    [row] = saved(web)
    assert row.active
    assert row.fires_from == NOW  # switching on never fires for what was decided while off

    response = web.post(f"/settings/filters/{filter_id}/delete", htmx=False)
    assert response.status_code == 303
    assert saved(web) == []


def test_settings_is_prefilled_from_the_dashboard_filters(web: Web) -> None:
    page = web.page("/settings", contract="only", q="python", max_applicants="50")
    assert re.search(r'<option value="only"\s+selected>', page)
    assert re.search(r'<option value="50"\s+selected>', page)
    assert 'name="q" value="python"' in page


def test_editing_a_filter_shows_its_values(web: Web) -> None:
    filter_id = add_filter(web, "Austin", 3, place="US-TX", contract="hide")
    page = web.page("/settings", edit=str(filter_id))
    assert 'name="label" value="Austin"' in page
    assert re.search(r'<option value="hide"\s+selected>', page)
    assert re.search(r'name="slot" value="3"\s+checked', page)


def test_a_filter_that_no_longer_reads_back_is_flagged(web: Web) -> None:
    filter_id = add_filter(web, "Old", 0)
    with web.sessions() as session:
        row = session.get(SavedFilter, filter_id)
        assert row is not None
        row.params = {**row.params, "exp": "2-4"}
        session.commit()
    page = html.unescape(web.page("/settings"))
    assert "no longer valid" in page
    assert "Old" not in re.sub(
        r"<section class=\"saved\".*?</section>", "", web.page("/"), flags=re.S
    )


def test_color_blind_mode_adds_shapes(web: Web) -> None:
    add_filter(web, "Austin", 2, place="US-TX")
    assert "▲" not in web.page("/")
    response = web.post("/settings/color-blind", {"color_blind": "1"}, htmx=False)
    assert response.status_code == 303
    assert "▲" in web.page("/")
    assert "▲" in web.page("/settings")
    web.post("/settings/color-blind", {}, htmx=False)
    assert "▲" not in web.page("/")


# --- the postings page ----------------------------------------------------------------------


def test_buttons_apply_a_saved_filter_with_a_badge_of_new_matches(web: Web) -> None:
    austin = add_filter(web, "Austin", 0, place="US-TX")
    old = web.add(locations=["Austin, TX"], first_seen_at=NOW - timedelta(hours=3))
    new = web.add(locations=["Austin, TX"], first_seen_at=NOW - timedelta(minutes=30))
    web.add(locations=["Seattle, WA"], first_seen_at=NOW - timedelta(minutes=30))

    page = html.unescape(web.page("/"))
    button = re.search(r'<a class="saved-filter[^"]*"[^>]*>.*?</a>', page, re.S)
    assert button is not None
    assert "Austin" in button.group(0)
    assert 'href="/?place=US-TX"' in button.group(0)
    assert f'data-viewed="/saved-filters/{austin}/viewed"' in button.group(0)
    assert re.search(r'class="badge-count"[^>]*>1<', button.group(0))  # new only, not old
    assert web.ids(place="US-TX") == [new, old]

    response = web.post(f"/saved-filters/{austin}/viewed")
    assert response.status_code == 204
    assert saved(web)[0].last_viewed_at == NOW
    assert "badge-count" not in web.page("/")


def test_the_button_for_the_current_filters_is_highlighted(web: Web) -> None:
    add_filter(web, "Austin", 0, place="US-TX")
    add_filter(web, "Contract", 1, contract="only")
    page = web.page("/", place="US-TX")
    current = re.findall(r'<a class="saved-filter current[^>]*>(.*?)</a>', page, re.S)
    assert len(current) == 1
    assert "Austin" in current[0]
    assert "saved-filter current" not in web.page("/")


def test_cards_show_the_bullets_of_the_filters_they_fit(web: Web) -> None:
    add_filter(web, "Austin", 0, place="US-TX")
    add_filter(web, "Contract", 1, contract="only")
    both = web.add(locations=["Austin, TX"], employment_type="contract")
    one = web.add(locations=["Austin, TX"], employment_type="full_time")
    none = web.add(locations=["Seattle, WA"])
    page = web.page("/")
    assert card(page, both).count('class="bullet slot-') == 2
    assert card(page, one).count('class="bullet slot-') == 1
    assert 'class="bullet slot-' not in card(page, none)


def test_save_these_filters_opens_settings_prefilled(web: Web) -> None:
    web.add()
    page = html.unescape(web.page("/", contract="only", sort="fresh"))
    link = re.search(r'<a class="save-filters" href="([^"]+)"', page)
    assert link is not None
    assert link.group(1) == "/settings?contract=only"
