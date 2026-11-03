"""M5b on the dashboard: the "Best resume match" sort and the closest resume line on a card."""

import html
import re

from tests.integration.conftest import Web
from tests.integration.test_web_postings import card


def test_sort_by_best_resume_match_puts_unscored_last(web: Web) -> None:
    close = web.add(title="A", score=0.5, resume_score=0.9, resume_line="Built Python services")
    unscored = web.add(title="B", score=0.95)
    far = web.add(title="C", score=0.8, resume_score=0.2, resume_line="Led a React rewrite")
    assert web.ids(sort="resume") == [close, far, unscored]
    assert re.search(r'<option value="resume"\s+selected>', web.page("/", sort="resume"))


def test_a_card_shows_the_closest_resume_line_escaped_and_cut(web: Web) -> None:
    line = "Built <b>Python</b> services " + "and more " * 30
    posting_id = web.add(resume_score=0.62, resume_line=line)
    plain_id = web.add(title="Plain")
    page = web.page("/")

    shown = card(page, posting_id)
    assert "<b>Python</b>" not in shown
    assert "&lt;b&gt;Python&lt;/b&gt;" in shown
    match = re.search(r'<span class="resume"[^>]*>(.*?)</span>', shown, re.DOTALL)
    assert match is not None
    text = html.unescape(match.group(1)).strip()
    assert text.startswith("Resume: Built <b>Python</b> services")
    assert len(text.removeprefix("Resume: ")) <= 160
    assert 'class="resume"' not in card(page, plain_id)


def test_the_score_tooltip_names_the_resume_part(web: Web) -> None:
    breakdown = {"title": 0.3, "stack": None, "resume": 0.2, "total": 0.5}
    posting_id = web.add(
        score=0.5, score_breakdown=breakdown, resume_score=0.8, resume_line="x y z"
    )
    assert "resume +0.20" in html.unescape(card(web.page("/"), posting_id))
