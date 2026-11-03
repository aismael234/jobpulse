"""M5.0 dashboard filters: reposts hidden from New by default, a maximum number of applicants
(unknown counts pass), contract work shown, hidden or alone, and every filter visible."""

import html
import re

from tests.integration.conftest import Web, posting_ids
from tests.integration.test_web_postings import card, nav_count

REASON = "first seen 12 days ago (requisition 1099719)"


def test_reposts_are_hidden_from_new_by_default(web: Web) -> None:
    fresh = web.add(score=0.6)
    repost = web.add(score=0.5, repost_reason=REASON)

    page = web.page("/")
    assert posting_ids(page) == [fresh]
    assert nav_count(page, "New") == 1  # the count is what the view shows

    shown = web.page("/", reposted="show")
    assert posting_ids(shown) == [fresh, repost]
    repost_card = html.unescape(card(shown, repost))
    assert "Reposted" in repost_card
    assert REASON in repost_card
    assert "Reposted" not in card(shown, fresh)


def test_reposts_you_kept_always_show(web: Web) -> None:
    saved = web.add(status="Saved", repost_reason=REASON)
    applied = web.add(status="Applied", repost_reason=REASON)
    assert web.ids(view="saved") == [saved]
    assert web.ids(view="applied") == [applied]


def test_the_include_reposted_box_is_in_new_and_remembers_its_state(web: Web) -> None:
    web.add()
    box = re.compile(r'<input type="checkbox" name="reposted" value="show"( checked)?>')
    found = box.search(web.page("/"))
    assert found is not None
    assert found.group(1) is None
    found = box.search(web.page("/", reposted="show"))
    assert found is not None
    assert found.group(1) == " checked"
    assert box.search(web.page("/", view="applied")) is None


def test_maximum_applicants_hides_known_crowds_and_lets_unknown_counts_through(web: Web) -> None:
    first = web.add(score=0.9, applicant_label="Be among the first 25 applicants", applicant_max=25)
    some = web.add(score=0.8, applicant_label="87 applicants", applicant_min=87, applicant_max=87)
    crowd = web.add(score=0.7, applicant_label="Over 200 applicants", applicant_min=200)
    unknown = web.add(score=0.6)

    assert web.ids() == [first, some, crowd, unknown]
    assert web.ids(max_applicants="200") == [first, some, unknown]
    assert web.ids(max_applicants="100") == [first, some, unknown]
    assert web.ids(max_applicants="50") == [first, unknown]
    assert web.ids(max_applicants="25") == [first, unknown]
    assert web.ids(max_applicants="7") == web.ids()  # not a choice: ignored


def test_contract_work_shown_hidden_or_alone(web: Web) -> None:
    full = web.add(score=0.9, employment_type="full_time")
    contract = web.add(score=0.8, employment_type="contract")
    unknown = web.add(score=0.7, employment_type=None)
    intern = web.add(score=0.6, employment_type="internship")

    assert web.ids() == [full, contract, unknown, intern]
    assert web.ids(contract="hide") == [full, unknown, intern]
    assert web.ids(contract="only") == [contract]
    assert web.ids(contract="sometimes") == web.ids()
    page = web.page("/")
    assert "Contract" in card(page, contract)
    assert "Contract" not in card(page, full)
    assert "Internship" in card(page, intern)


def test_every_filter_is_visible_without_a_fold(web: Web) -> None:
    web.add()
    page = web.page("/")
    filter_bar = page[page.index('<form class="filters"') : page.index("</form>")]
    assert "<details" not in filter_bar
    assert "More filters" not in page
    for name in (
        "q",
        "place",
        "pushed",
        "company",
        "source",
        "remote",
        "pay",
        "pay_listed",
        "exp",
        "min_score",
        "open",
        "max_applicants",
        "contract",
        "reposted",
    ):
        assert f'name="{name}"' in page, name


def test_filters_survive_paging_links(web: Web) -> None:
    for _ in range(51):
        web.add(employment_type="contract", applicant_min=10, applicant_max=10)
    page = html.unescape(web.page("/", contract="only", max_applicants="50", reposted="show"))
    next_link = re.search(r'href="([^"]*)" rel="next"', page)
    assert next_link is not None
    for part in ("contract=only", "max_applicants=50", "reposted=show", "page=2"):
        assert part in next_link.group(1)
