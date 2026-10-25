"""Which countries a normalized location label is in, for ``filters.locations.countries``."""

import pytest

from jobpulse.core.countries import ANYWHERE, country_code, label_countries


@pytest.mark.parametrize(
    "label",
    [
        "San Francisco, CA",
        "Burnsville, MN",
        "Research Triangle Park, NC",
        "Washington, DC",
        "San Juan, PR",
        "Long Beach, California",
        "Austin, Texas",
        "Atlanta, Georgia",
        "Georgia, United States",
        "California",
        "Massachusetts - Boston",
        "New York, NY (HQ)",
        "US, CA, Santa Clara",
        "USA - California - Sunnyvale",
        "USA",
        "United States",
        "United States of America",
        "U.S.",
        "San Francisco Bay Area",
        "Greater Seattle Area",
        "New York City Metropolitan Area",
        "Dallas-Fort Worth Metroplex",
        "Sunnyvale",
        "Remote (US)",
        "Remote (San Francisco/CA)",
        "Lebanon, NH",
        "Paris, TX",
        "Dublin, CA",
        "Vancouver, WA",
    ],
)
def test_us_labels(label: str) -> None:
    assert label_countries(label) == {"US"}


@pytest.mark.parametrize(
    ("label", "code"),
    [
        ("Nigeria", "NG"),
        ("Lousado, Braga, Portugal", "PT"),
        ("Shanghai, Shanghai, China", "CN"),
        ("London, England, UK", "GB"),
        ("London, UK", "GB"),
        ("London", "GB"),
        ("Bristol, UK", "GB"),
        ("India", "IN"),
        ("Bangalore, Karnataka, India", "IN"),
        ("Pune", "IN"),
        ("Pune, Maharashtra", "IN"),
        ("Seoul, South Korea", "KR"),
        ("Ontario, Canada", "CA"),
        ("Toronto, Ontario, Canada", "CA"),
        ("Toronto, ON", "CA"),
        ("London, ON", "CA"),
        ("Vancouver", "CA"),
        ("Greater Toronto Area", "CA"),
        ("Ho Chi Minh City, Vietnam", "VN"),
        ("Beer Yaakov, Center District, Israel", "IL"),
        ("Tel Aviv-Yafo, Tel Aviv District, Israel", "IL"),
        ("Singapore, Singapore", "SG"),
        ("Singapore", "SG"),
        ("Jakarta, Indonesia", "ID"),
        ("Tokyo, Japan", "JP"),
        ("Japan", "JP"),
        ("Barcelona, Catalonia, Spain", "ES"),
        ("Dublin", "IE"),
        ("Dublin, Ireland", "IE"),
        ("Paris", "FR"),
        ("Stockholm", "SE"),
        ("Hong Kong SAR", "HK"),
        ("Remote (India)", "IN"),
        ("Remote (Canada)", "CA"),
        ("Remote (UK)", "GB"),
        ("Remote (Germany)", "DE"),
    ],
)
def test_labels_outside_the_us(label: str, code: str) -> None:
    assert label_countries(label) == {code}


@pytest.mark.parametrize(
    "label",
    [
        "Remote (Global)",
        "Remote (World Wide)",
        "Remote (Worldwide)",
        "Remote (Anywhere)",
        "Home based - Worldwide",
        "Worldwide",
    ],
)
def test_worldwide_remote_is_anywhere(label: str) -> None:
    assert label_countries(label) == ANYWHERE


def countries(label: str) -> frozenset[str]:
    found = label_countries(label)
    assert found is not None
    return found


def test_remote_regions_name_their_countries() -> None:
    assert label_countries("Remote (US/Canada)") == {"US", "CA"}
    assert {"US", "CA", "MX"} <= countries("Remote (North America)")
    assert {"US", "CA", "BR"} <= countries("Home Based - Americas")
    assert {"GB", "DE", "AE", "ZA"} <= countries("Home based - EMEA")
    assert {"GB", "DE"} <= countries("Remote (Europe)")
    assert "DE" in countries("Remote (EU)")
    assert "GB" not in countries("Remote (EU)")
    assert {"JP", "AU", "IN"} <= countries("Remote (APAC)")
    assert {"BR", "MX"} <= countries("Remote (LATAM)")
    for label in ("Home based - EMEA", "Remote (Europe)", "Remote (APAC)", "Remote (LATAM)"):
        assert "US" not in countries(label)


@pytest.mark.parametrize(
    ("label", "codes"),
    [
        ("Dallas TX", {"US"}),
        ("901 Technology Way, Libertyville IL", {"US"}),
        ("Northern VA", {"US"}),
        ("East Texas", {"US"}),
        ("San Francisco HQ", {"US"}),
        ("SF Office", {"US"}),
        ("US-SF", {"US"}),
        ("Dublin Ireland Office", {"IE"}),
        ("Australia- Melbourne", {"AU"}),
        ("Wichita Metro Area", {"US"}),
        ("United States and Canada", {"US", "CA"}),
        ("Washington, DC AND Montreal, CANADA", {"US", "CA"}),
        ("SEA, SF, NY, Toronto", {"US", "CA"}),
        ("Trinidad and Tobago", {"TT"}),
        ("Remote (Work Home) US)", {"US"}),
        ("US > Arizona > Phoenix", {"US"}),
        ("Remote (Germany (Berlin)", {"DE"}),
    ],
)
def test_labels_seen_in_real_postings(label: str, codes: set[str]) -> None:
    """Forms found by running the tables over every stored label on 2026-10-25."""
    assert label_countries(label) == codes


@pytest.mark.parametrize(
    "label",
    ["Remote", "Georgia", "Cambridge", "Pimpri-Chinchwad Area", "Main Campus", ""],
)
def test_labels_without_a_known_country(label: str) -> None:
    """A label that names no place in the tables, or a name shared by a US state and a
    country (Georgia), or by US and foreign cities (Cambridge), is unknown."""
    assert label_countries(label) is None


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("US", "US"),
        ("us", "US"),
        ("USA", "US"),
        ("United States", "US"),
        ("UK", "GB"),
        ("gb", "GB"),
        ("Britain", "GB"),
        ("CA", "CA"),
        ("Canada", "CA"),
        ("Germany", "DE"),
        ("South Korea", "KR"),
    ],
)
def test_country_codes_from_config(name: str, code: str) -> None:
    assert country_code(name) == code


@pytest.mark.parametrize("name", ["Narnia", "EU", "EMEA", "XX", "California", ""])
def test_unknown_country_names(name: str) -> None:
    assert country_code(name) is None
