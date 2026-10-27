"""Reading the dashboard's filters and return paths: lenient with blanks, strict with targets."""

from dataclasses import replace
from urllib.parse import parse_qsl, urlsplit

import pytest

from jobpulse.db.dashboard import PostingQuery
from jobpulse.web.routes import parse_query, safe_return_to
from jobpulse.web.views import link


def test_no_parameters_mean_the_new_view_by_score() -> None:
    assert parse_query({}) == PostingQuery(status="New", sort="score", page=1)


def test_every_filter_is_read() -> None:
    query = parse_query(
        {
            "view": "saved",
            "source": "linkedin",
            "company": " Acme ",
            "remote": "yes",
            "min_score": "0.4",
            "q": "  python  ",
            "open": "1",
            "sort": "fresh",
            "page": "3",
        }
    )
    assert query == PostingQuery(
        status="Saved",
        source="linkedin",
        company="Acme",
        remote=True,
        min_score=0.4,
        text="python",
        open_only=True,
        sort="fresh",
        page=3,
    )


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"view": "Applied"}, PostingQuery(status="Applied")),
        ({"view": "dismissed"}, PostingQuery(status="Dismissed")),
        ({"remote": "no"}, PostingQuery(remote=False)),
        # blanks, as an HTML form submits them, are ignored
        ({"source": "", "company": " ", "min_score": "", "q": "", "remote": ""}, PostingQuery()),
        # unreadable or unknown values fall back to the defaults instead of failing
        (
            {"view": "archived", "source": "monster", "min_score": "high", "sort": "random"},
            PostingQuery(),
        ),
        ({"page": "0"}, PostingQuery()),
        ({"page": "-4"}, PostingQuery()),
        ({"page": "two"}, PostingQuery()),
        ({"min_score": "nan"}, PostingQuery()),
        ({"min_score": "-0.5"}, PostingQuery(min_score=0.0)),
    ],
)
def test_lenient_parsing(params: dict[str, str], expected: PostingQuery) -> None:
    assert parse_query(params) == expected


def test_location_pushed_time_and_pay_are_read() -> None:
    query = parse_query(
        {"place": "us-tx/ Austin ", "pushed": "over-3d", "pay": "120", "pay_listed": "1"}
    )
    assert query == PostingQuery(
        place="US-TX/Austin", pushed="over-3d", min_pay=120, pay_listed=True
    )
    assert parse_query({"sort": "pushed"}) == PostingQuery(sort="pushed")


@pytest.mark.parametrize(
    "params",
    [
        {"place": "Texas"},
        {"place": "US-ZZ"},
        {"pushed": "2h"},
        {"pay": "lots"},
        {"pay": "0"},
        {"pay": "-5"},
        {"pay": "nan"},
        {"pay_listed": "maybe"},
    ],
)
def test_unreadable_location_time_and_pay_are_ignored(params: dict[str, str]) -> None:
    assert parse_query(params) == PostingQuery()


def test_links_keep_the_new_filters() -> None:
    query = PostingQuery(
        place="US-TX/Austin", pushed="6h", min_pay=150, pay_listed=True, sort="pushed"
    )
    url = link(query, page=2)
    assert parse_query(dict(parse_qsl(urlsplit(url).query))) == replace(query, page=2)
    assert link(PostingQuery()) == "/"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/?view=saved&sort=fresh", "/?view=saved&sort=fresh"),
        ("/watchlist", "/watchlist"),
        (None, "/"),
        ("", "/"),
        ("https://evil.example/", "/"),
        ("//evil.example/path", "/"),
        ("/\\evil.example", "/"),
        ("javascript:alert(1)", "/"),
        ("watchlist", "/"),
        ("/ok\r\nSet-Cookie: x=1", "/"),
    ],
)
def test_return_paths_stay_on_the_dashboard(value: str | None, expected: str) -> None:
    assert safe_return_to(value) == expected
