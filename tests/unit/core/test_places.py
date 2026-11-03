"""Where a label is below its countries (state, city, metro area), for the dashboard's
location filter: keys, matching, and the options with their counts."""

import pytest

from jobpulse.core.countries import ANYWHERE
from jobpulse.core.places import (
    Spot,
    label_place,
    parse_place_key,
    place_label,
    place_options,
    posting_in_place,
)

US = frozenset({"US"})


@pytest.mark.parametrize(
    ("label", "state", "city"),
    [
        ("San Francisco, CA", "US-CA", "San Francisco"),
        ("Austin, Texas", "US-TX", "Austin"),
        ("Spring, TX", "US-TX", "Spring"),  # a town the tables don't know, with its state
        ("US, CA, Santa Clara", "US-CA", "Santa Clara"),  # Workday
        ("USA - California - Sunnyvale", "US-CA", "Sunnyvale"),
        ("US > Arizona > Phoenix", "US-AZ", "Phoenix"),
        ("Massachusetts - Boston", "US-MA", "Boston"),
        ("New York, NY", "US-NY", "New York"),
        ("New York, New York", "US-NY", "New York"),
        ("Washington, DC", "US-DC", "Washington"),
        ("Washington D.C.", "US-DC", "Washington"),
        ("Detroit Area, Michigan", "US-MI", "Detroit"),
        ("Greater Seattle Area", "US-WA", "Seattle"),
        ("San Francisco County, CA", "US-CA", "San Francisco"),
        ("Sunnyvale", "US-CA", "Sunnyvale"),  # bare cities take their state from the table
        ("Mountain View", "US-CA", "Mountain View"),
        ("Chicago, United States", "US-IL", "Chicago"),
        ("New York City Metropolitan Area", "US-NY", "New York"),
        ("SF Office", "US-CA", "San Francisco"),
        ("NYC Office", "US-NY", "New York"),
        ("South SF", "US-CA", "South San Francisco"),
        ("US-San Francisco", "US-CA", "San Francisco"),
        ("St Louis, MO", "US-MO", "St. Louis"),
        ("Dallas TX", "US-TX", "Dallas"),
        ("Arlington", "US-VA", "Arlington"),  # 135 stored "Arlington, VA", none in TX
        ("Louisville", None, "Louisville"),  # CO and KY both common: no state
        ("Toronto, Ontario, Canada", "CA-ON", "Toronto"),
    ],
)
def test_state_and_city(label: str, state: str | None, city: str) -> None:
    place = label_place(label)
    assert place.spots, label
    assert (place.spots[0].state, place.spots[0].city) == (state, city)


@pytest.mark.parametrize(
    ("label", "state"),
    [
        ("VA, USA", "US-VA"),
        ("California", "US-CA"),
        ("Georgia, United States", "US-GA"),
        ("New York, USA", "US-NY"),
        ("Northern VA", "US-VA"),
        ("San Francisco Bay Area", "US-CA"),
        ("Silicon Valley, CA", "US-CA"),
        ("US-CA", "US-CA"),
        ("Raleigh-Durham-Chapel Hill Area", "US-NC"),
        ("Miami-Fort Lauderdale Area", "US-FL"),
    ],
)
def test_state_without_a_city(label: str, state: str) -> None:
    assert [(spot.state, spot.city) for spot in label_place(label).spots] == [(state, None)]


@pytest.mark.parametrize(
    "label",
    [
        "Remote (US)",
        "Remote",
        "USA",
        "United States",
        "United States of America",
        "Remote (Global)",
        "Remote (US/Canada)",
        "North America",
        "NerdWallet US",  # a country at the end of a word isn't a city
        "In-Office",
        "Sausalito",  # not in the tables: its country isn't known either
    ],
)
def test_labels_without_a_place_below_the_country(label: str) -> None:
    assert label_place(label).spots == ()


def test_countries_come_from_the_country_tables() -> None:
    assert label_place("Austin, TX").countries == US
    assert label_place("Remote (Global)").countries == ANYWHERE
    assert label_place("Remote").countries is None
    assert label_place("London, England, UK").countries == frozenset({"GB"})


@pytest.mark.parametrize(
    ("label", "city"),
    [
        ("London, England, UK", "London"),
        ("Tokyo, Japan", "Tokyo"),
        ("Beer Yaakov, Center District, Israel", "Beer Yaakov"),  # city, region, country
        ("Pune", "Pune"),
        ("Dublin", "Dublin"),  # Ireland
    ],
)
def test_cities_abroad_have_no_state(label: str, city: str) -> None:
    (spot,) = label_place(label).spots
    assert spot == Spot(city=city)


def test_a_list_of_cities_is_every_city_in_it() -> None:
    assert [spot.city for spot in label_place("San Francisco, Seattle").spots] == [
        "San Francisco",
        "Seattle",
    ]
    assert [spot.city for spot in label_place("NYC, SF").spots] == ["New York", "San Francisco"]


@pytest.mark.parametrize(
    ("label", "metro"),
    [
        ("San Francisco, CA", "bay-area"),
        ("Sunnyvale", "bay-area"),
        ("Dublin, CA", "bay-area"),
        ("San Francisco Bay Area", "bay-area"),
        ("Silicon Valley, CA", "bay-area"),
        ("Brooklyn, NY", "nyc"),
        ("Jersey City, NJ", "nyc"),
        ("Annapolis Junction, MD", "dc"),
        ("Arlington, VA", "dc"),
        ("Washington DC-Baltimore Area", "dc"),
        ("Northern VA", "dc"),
        ("Redmond, WA", "seattle"),
        ("Cambridge, MA", "boston"),
        ("Santa Monica, CA", "la"),
        ("Plano, TX", "dfw"),
        ("Round Rock, TX", "austin"),
        ("Boulder, CO", "denver"),
        ("Durham, NC", "triangle"),
        ("Alpharetta, GA", "atlanta"),
        ("Rosemont, IL", "chicago"),
    ],
)
def test_metro_areas(label: str, metro: str) -> None:
    assert metro in {spot.metro for spot in label_place(label).spots}


@pytest.mark.parametrize("label", ["Dublin", "Spring, TX", "Portland, ME", "Remote (US)"])
def test_places_outside_any_metro(label: str) -> None:
    assert all(spot.metro is None for spot in label_place(label).spots)


@pytest.mark.parametrize(
    ("value", "key"),
    [
        ("US", "US"),
        ("us", "US"),
        ("US-TX", "US-TX"),
        ("ca-on", "CA-ON"),
        ("metro:bay-area", "metro:bay-area"),
        ("US-TX/Austin", "US-TX/Austin"),
        ("US-TX/ Round  Rock ", "US-TX/Round Rock"),
        ("GB/London", "GB/London"),
        ("", None),
        ("XX", None),  # no such country
        ("US-ZZ", None),  # no such state
        ("GB-EN", None),  # states only in the US and Canada
        ("metro:atlantis", None),
        ("US-TX/", None),
        ("US/" + "x" * 200, None),
        ("Texas", None),
    ],
)
def test_place_keys_are_read_leniently(value: str, key: str | None) -> None:
    assert parse_place_key(value) == key


@pytest.mark.parametrize(
    ("key", "label"),
    [
        ("US", "United States"),
        ("US-CA", "California"),
        ("CA-ON", "Ontario (Canada)"),
        ("metro:bay-area", "San Francisco Bay Area"),
        ("US-TX/Austin", "Austin, TX"),
        ("CA-ON/Toronto", "Toronto, ON"),
        ("GB/London", "London, United Kingdom"),
        ("US/Louisville", "Louisville, United States"),
    ],
)
def test_place_labels(key: str, label: str) -> None:
    assert place_label(key) == label


def test_matching_a_posting() -> None:
    labels = ["Austin, TX", "Remote (US)"]
    for key in ("US", "US-TX", "US-TX/Austin", "metro:austin", "US-TX/austin"):
        assert posting_in_place(labels, key), key
    for key in ("US-CA", "US-TX/Dallas", "metro:dfw", "GB"):
        assert not posting_in_place(labels, key), key


def test_remote_labels_match_countries_only() -> None:
    assert posting_in_place(["Remote (US)"], "US")
    assert not posting_in_place(["Remote (US)"], "US-CA")
    assert posting_in_place(["Remote (Global)"], "US")  # worldwide remote, as the poller reads it
    assert posting_in_place(["Remote (EMEA)"], "DE")
    assert not posting_in_place(["Remote (EMEA)"], "US")
    assert not posting_in_place(["Remote"], "US")
    assert not posting_in_place([], "US")


def _options(groups: object) -> dict[str, tuple[str, int]]:
    return {
        option.key: (option.label, option.count)
        for group in groups  # type: ignore[attr-defined]
        for option in group.options
    }


def test_options_count_postings_per_place() -> None:
    postings = [
        ["San Francisco, CA"],
        ["San Francisco, CA", "New York, NY"],
        ["Sunnyvale"],
        ["Remote (US)"],
        ["Remote (Global)"],
        ["Remote (EMEA)"],
        ["London, England, UK"],
        [],
    ]
    groups = place_options(postings, selected=None)
    assert [group.label for group in groups] == [
        "Countries",
        "States and provinces",
        "Metro areas",
        "Cities",
    ]
    options = _options(groups)
    # worldwide remote counts toward every listed country; EMEA lists none of its countries
    assert options["US"] == ("United States", 5)
    assert options["GB"] == ("United Kingdom", 3)
    assert "DE" not in options
    assert options["US-CA"] == ("California", 3)
    assert options["US-NY"] == ("New York", 1)
    assert options["metro:bay-area"] == ("San Francisco Bay Area", 3)
    assert options["metro:nyc"] == ("New York City area", 1)
    assert options["US-CA/San Francisco"] == ("San Francisco, CA", 2)
    assert options["US-CA/Sunnyvale"] == ("Sunnyvale, CA", 1)
    assert options["GB/London"] == ("London, United Kingdom", 1)
    labels = [option.label for option in groups[3].options]
    assert labels == sorted(labels, key=str.casefold)


def test_a_selected_place_stays_listed_when_nothing_matches() -> None:
    options = _options(place_options([["Austin, TX"]], selected="US-WA/Seattle"))
    assert options["US-WA/Seattle"] == ("Seattle, WA", 0)
    assert _options(place_options([], selected=None)) == {}
