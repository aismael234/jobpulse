"""Item 11 (locations): multi-city strings, remote forms, aliases."""

import pytest

from jobpulse.core.locations import (
    location_key,
    location_summary,
    merge_aliases,
    normalize_locations,
    short_label,
)


@pytest.mark.parametrize(
    "raw",
    [
        "Remote - US",
        "Remote – US",
        "Remote (US)",
        "US Remote",
        "Remote, United States",
        "Remote in the US",
        "remote - usa",
        "United States (Remote)",
        "Remote - U.S.",
        "Remote-US",
    ],
)
def test_us_remote_forms(raw: str) -> None:
    assert normalize_locations([raw]) == (("Remote (US)",), True)


def test_plain_remote() -> None:
    assert normalize_locations(["Remote"]) == (("Remote",), True)


def test_remote_with_another_region() -> None:
    assert normalize_locations(["Remote - Europe"]) == (("Remote (Europe)",), True)


def test_slash_inside_parentheses_does_not_split() -> None:
    assert normalize_locations(["Remote (US/Canada)"]) == (("Remote (US/Canada)",), True)


def test_slash_separated_aliases() -> None:
    assert normalize_locations(["SF / NYC"]) == (("San Francisco, CA", "New York, NY"), False)


def test_semicolon_separated_cities() -> None:
    assert normalize_locations(["San Francisco, CA; New York, NY"]) == (
        ("San Francisco, CA", "New York, NY"),
        False,
    )


def test_comma_separated_city_state_pairs() -> None:
    assert normalize_locations(["San Francisco, CA, New York, NY"]) == (
        ("San Francisco, CA", "New York, NY"),
        False,
    )


def test_city_and_remote() -> None:
    assert normalize_locations(["SF / Remote"]) == (("San Francisco, CA", "Remote"), True)


def test_or_and_pipe_separators() -> None:
    labels, _ = normalize_locations(["Seattle, WA or Austin, TX | Denver, CO"])
    assert labels == ("Seattle, WA", "Austin, TX", "Denver, CO")


def test_several_raw_strings_are_flattened_and_deduplicated() -> None:
    assert normalize_locations(["SF", "San Francisco, CA", "Remote - US"]) == (
        ("San Francisco, CA", "Remote (US)"),
        True,
    )


def test_user_alias_is_applied() -> None:
    aliases = merge_aliases({"HQ": "Austin, TX"})
    assert normalize_locations(["HQ"], aliases) == (("Austin, TX",), False)
    assert normalize_locations(["NYC"], aliases) == (("New York, NY",), False)


def test_hybrid_marker_is_dropped() -> None:
    assert normalize_locations(["Hybrid - SF"]) == (("San Francisco, CA",), False)


def test_unknown_location_is_kept_as_written() -> None:
    assert normalize_locations(["Zürich, Switzerland"]) == (("Zürich, Switzerland",), False)


def test_no_location() -> None:
    assert normalize_locations([]) == ((), None)
    assert normalize_locations([None, "  "]) == ((), None)


@pytest.mark.parametrize(
    ("label", "short"),
    [
        ("San Francisco, CA", "SF"),
        ("New York, NY", "NYC"),
        ("Remote (US)", "Remote"),
        ("Remote", "Remote"),
        ("Remote (Europe)", "Remote Europe"),
        ("London, UK", "London"),
    ],
)
def test_short_label(label: str, short: str) -> None:
    assert short_label(label) == short


def test_location_summary() -> None:
    assert location_summary(["San Francisco, CA", "Remote (US)"]) == "SF / Remote"
    assert (
        location_summary(["San Francisco, CA", "New York, NY", "Seattle, WA", "Austin, TX"])
        == "SF / NYC +2"
    )
    assert location_summary([]) == ""


def test_location_key_is_city_level() -> None:
    assert location_key("San Francisco, CA") == "san francisco"
    assert location_key("San Francisco, California, United States") == "san francisco"
    assert location_key("Remote (US)") == "remote"
    assert location_key(None) == ""
