"""M5.1: a saved filter keeps every filter written out, defaults included, and reads back
only when its parameters round-trip exactly."""

from jobpulse.db.dashboard import PostingQuery
from jobpulse.db.query_params import FILTER_KEYS, filter_params, parse_query, saved_query


def test_every_filter_is_written_out_with_defaults() -> None:
    assert filter_params(PostingQuery()) == {
        "q": "",
        "place": "",
        "company": "",
        "source": "",
        "remote": "",
        "exp": "",
        "min_score": "",
        "pay": "",
        "pay_listed": "0",
        "open": "0",
        "reposted": "hide",
        "max_applicants": "",
        "contract": "show",
    }
    assert tuple(filter_params(PostingQuery())) == FILTER_KEYS


def test_view_sort_page_and_pushed_are_not_kept() -> None:
    query = parse_query(
        {"view": "saved", "sort": "fresh", "page": "3", "pushed": "6h", "contract": "only"}
    )
    params = filter_params(query)
    assert not {"view", "sort", "page", "pushed"} & set(params)
    assert params["contract"] == "only"


def test_a_query_round_trips() -> None:
    url = {
        "q": "python",
        "place": "US-CA",
        "company": "Acme",
        "source": "greenhouse",
        "remote": "yes",
        "exp": "3-5",
        "min_score": "0.5",
        "pay": "150",
        "pay_listed": "1",
        "open": "1",
        "reposted": "show",
        "max_applicants": "50",
        "contract": "hide",
    }
    params = filter_params(parse_query(url))
    query, error = saved_query(params)
    assert error is None
    assert query is not None
    assert query == parse_query({**url, "view": "new"})
    assert filter_params(query) == params


def test_saved_parameters_that_no_longer_read_back_are_invalid() -> None:
    good = filter_params(PostingQuery())
    for broken in (
        {**good, "exp": "2-4"},  # a choice that went away
        {**good, "flavor": "x"},  # a filter that was renamed
        {key: value for key, value in good.items() if key != "contract"},  # a missing one
        "not a mapping",
        {**good, "q": 5},
    ):
        query, error = saved_query(broken)
        assert query is None
        assert error
