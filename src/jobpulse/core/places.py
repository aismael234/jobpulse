"""Where a normalized location label is below its countries, for the dashboard's location
filter: the state or province (US and Canada), the city, and the metro area.

"Austin, Texas", "US, CA, Santa Clara" and "Sunnyvale" name a state and a city; "VA, USA" and
"San Francisco Bay Area" only a state; "Remote (US)" and "USA" nothing below the country. A
city the tables don't know counts only next to its state or country ("Spring, TX", "Beer
Yaakov, Center District, Israel"), so "NerdWallet US" is no city. A bare city takes its state
from a table; a name common in two states (Louisville) gets none.

Place keys, short enough for a URL: ``US`` (a country), ``US-TX`` (a state), ``metro:bay-area``,
and ``US-TX/Austin`` or ``GB/London`` (a city, without the state when it isn't known).
"""

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from jobpulse.core.countries import (
    ANYWHERE,
    ISO_CODES,
    city_country,
    country_code,
    country_name,
    is_remote_label,
    label_countries,
    label_parts,
    names_country_or_region,
    split_pieces,
    subdivision_key,
    subdivision_name,
    trim_noise,
)
from jobpulse.core.text import collapse_whitespace

METRO_PREFIX: Final = "metro:"
# A label naming more countries than this ("Remote (EMEA)") still matches each of them, but
# doesn't add them to the list of countries to pick from.
MAX_LISTED_COUNTRIES: Final = 3
MAX_CITY_LENGTH: Final = 80


@dataclass(frozen=True, slots=True)
class Spot:
    """One place a label names below the country."""

    state: str | None = None  # "US-TX", "CA-ON"
    city: str | None = None  # "Austin"
    metro: str | None = None  # "bay-area"


@dataclass(frozen=True, slots=True)
class Place:
    countries: frozenset[str] | None  # as label_countries says
    spots: tuple[Spot, ...] = ()


@dataclass(frozen=True, slots=True)
class PlaceOption:
    key: str
    label: str
    count: int  # postings


@dataclass(frozen=True, slots=True)
class PlaceGroup:
    label: str
    options: tuple[PlaceOption, ...]


def _subdivision_table(text: str) -> dict[str, str]:
    """``US-CA City|City`` lines → {casefolded city: "US-CA"}."""
    table: dict[str, str] = {}
    for line in text.strip().splitlines():
        key, _, names = line.strip().partition(" ")
        for name in names.split("|"):
            table[name.strip().casefold()] = key
    return table


# Bare city names and their state. Where stored postings name one state almost always, it is
# that one (Arlington VA 135 to 0, Columbia MD 44 to 4, Kansas City MO 36 to 2, Portland OR);
# Louisville (CO and KY) is left out.
_CITY_STATES: Final = _subdivision_table(
    """
    US-CA San Francisco|Los Angeles|San Jose|Sunnyvale|Mountain View|Palo Alto|Menlo Park|Redwood City|Santa Clara|Cupertino|San Mateo|Foster City|South San Francisco|Oakland|Berkeley|Emeryville|Fremont|Pleasanton|Milpitas|Los Gatos|Burlingame|San Bruno|Livermore|San Diego|Irvine|Santa Monica|Culver City|El Segundo|Long Beach|Pasadena|Burbank|Torrance|Hawthorne|Huntington Beach|Santa Barbara|Goleta|Carlsbad|Sacramento|San Carlos|Daly City
    US-NY New York|Brooklyn
    US-WA Seattle|Bellevue|Redmond|Kirkland|Everett|Tacoma|Spokane
    US-TX Austin|Dallas|Houston|San Antonio|Fort Worth|Plano|Irving|Frisco|Round Rock
    US-MA Boston|Somerville|Waltham
    US-IL Chicago
    US-CO Denver|Boulder|Colorado Springs
    US-DC Washington
    US-OR Portland
    US-UT Salt Lake City|Lehi|Provo
    US-AZ Phoenix|Scottsdale|Tempe|Chandler
    US-NV Las Vegas
    US-GA Atlanta
    US-FL Miami|Tampa|Orlando|Jacksonville|Fort Lauderdale|Boca Raton
    US-NC Raleigh|Durham|Chapel Hill|Cary|Morrisville|Research Triangle Park|Charlotte
    US-TN Nashville|Memphis
    US-AL Huntsville
    US-PA Pittsburgh|Philadelphia
    US-MD Baltimore|Annapolis Junction|Fort Meade|Columbia|Bethesda
    US-VA Reston|Herndon|McLean|Tysons|Chantilly|Arlington|Virginia Beach
    US-MI Ann Arbor|Detroit
    US-MN Minneapolis|St. Paul
    US-MO St. Louis|Kansas City
    US-OH Columbus|Cleveland|Cincinnati
    US-IN Indianapolis
    US-WI Madison|Milwaukee
    US-ID Boise
    US-NE Omaha
    US-IA Des Moines
    US-OK Oklahoma City|Tulsa
    US-LA New Orleans
    US-NM Albuquerque
    US-HI Honolulu
    US-AK Anchorage
    US-NJ Jersey City|Hoboken|Newark|Princeton
    US-CT Stamford|Hartford
    US-RI Providence
    US-MS Southaven
    US-KS Wichita
    CA-ON Toronto|Ottawa|Waterloo|Kitchener|Mississauga|Markham
    CA-BC Vancouver|Burnaby
    CA-QC Montreal|Montréal|Quebec City|Sherbrooke|Longueuil|Gatineau
    CA-AB Calgary|Edmonton
    CA-MB Winnipeg
    CA-NS Halifax
    """
)

# Other spellings of a city, casefolded, and the name it goes by.
_CITY_NAMES: Final = {
    "sf": "San Francisco",
    "san fran": "San Francisco",
    "south sf": "South San Francisco",
    "nyc": "New York",
    "new york city": "New York",
    "manhattan": "New York",
    "la": "Los Angeles",
    "washington dc": "Washington",
    "washington d.c.": "Washington",
    "washington d.c": "Washington",
    "st louis": "St. Louis",
    "saint louis": "St. Louis",
    "st paul": "St. Paul",
    "saint paul": "St. Paul",
    "münchen": "Munich",
    "bengaluru": "Bangalore",
    "montréal": "Montreal",
}

# Areas named in labels, casefolded: their state and metro area.
_AREAS: Final[dict[str, tuple[str | None, str | None]]] = {
    **dict.fromkeys(
        (
            "san francisco bay area",
            "san francisco bay",
            "sf bay area",
            "bay area",
            "silicon valley",
            "peninsula",
            "south bay",
            "east bay",
        ),
        ("US-CA", "bay-area"),
    ),
    **dict.fromkeys(
        ("socal", "southern california", "norcal", "northern california"), ("US-CA", None)
    ),
    **dict.fromkeys(
        ("research triangle", "triangle", "raleigh-durham", "raleigh-durham-chapel hill"),
        ("US-NC", "triangle"),
    ),
    "miami-fort lauderdale": ("US-FL", None),
    **dict.fromkeys(("northern va", "northern virginia", "nova"), ("US-VA", "dc")),
    **dict.fromkeys(
        ("washington dc-baltimore", "washington dc-baltimore area", "dmv"), (None, "dc")
    ),
    **dict.fromkeys(("dallas-fort worth", "dfw"), ("US-TX", "dfw")),
    "tri-state": (None, "nyc"),
    "tri-state area": (None, "nyc"),
}

# Metro areas: their name and member cities, by state.
_METROS: Final[dict[str, tuple[str, dict[str, str]]]] = {
    "bay-area": (
        "San Francisco Bay Area",
        {
            "US-CA": "San Francisco|South San Francisco|Oakland|Berkeley|Emeryville|Alameda|"
            "Richmond|San Leandro|Hayward|Union City|Fremont|Newark|Milpitas|San Jose|"
            "Santa Clara|Sunnyvale|Mountain View|Palo Alto|Los Altos|Menlo Park|Redwood City|"
            "Redwood Shores|San Carlos|Belmont|San Mateo|Foster City|Burlingame|San Bruno|"
            "Millbrae|Daly City|Brisbane|Cupertino|Campbell|Los Gatos|Saratoga|Pleasanton|"
            "Dublin|Livermore|San Ramon|Walnut Creek|Concord|Danville|Sausalito|Mill Valley|"
            "Corte Madera|San Rafael|Novato"
        },
    ),
    "nyc": (
        "New York City area",
        {
            "US-NY": "New York|Brooklyn|Queens|Bronx|Staten Island|Long Island City|"
            "White Plains|Yonkers",
            "US-NJ": "Jersey City|Hoboken|Newark|Weehawken|Fort Lee|Secaucus|Paramus",
            "US-CT": "Stamford|Greenwich|Norwalk",
        },
    ),
    "seattle": (
        "Seattle area",
        {
            "US-WA": "Seattle|Bellevue|Redmond|Kirkland|Bothell|Everett|Renton|Kent|Tacoma|"
            "Issaquah|Lynnwood|Tukwila"
        },
    ),
    "dc": (
        "Washington DC area",
        {
            "US-DC": "Washington",
            "US-VA": "Arlington|Alexandria|McLean|Tysons|Reston|Herndon|Chantilly|Fairfax|"
            "Vienna|Falls Church|Sterling|Ashburn|Dulles|Leesburg|Manassas|Springfield|"
            "Quantico",
            "US-MD": "Bethesda|Rockville|Gaithersburg|Germantown|Silver Spring|College Park|"
            "Columbia|Annapolis Junction|Fort Meade|Laurel|Jessup|Odenton|Hanover|Linthicum|"
            "Baltimore|Annapolis|Landover|Greenbelt|Lanham",
        },
    ),
    "boston": (
        "Boston area",
        {
            "US-MA": "Boston|Cambridge|Somerville|Waltham|Lexington|Burlington|Bedford|"
            "Woburn|Quincy|Newton|Needham|Watertown|Wilmington|Marlborough|Hopkinton|"
            "Framingham|Natick|Andover|Lowell"
        },
    ),
    "la": (
        "Los Angeles area",
        {
            "US-CA": "Los Angeles|Santa Monica|Culver City|El Segundo|Playa Vista|Venice|"
            "Pasadena|Burbank|Glendale|Long Beach|Torrance|Hawthorne|Redondo Beach|"
            "Manhattan Beach|Irvine|Costa Mesa|Newport Beach|Santa Ana|Anaheim|"
            "Huntington Beach|Rancho Santa Margarita|Carson|Inglewood|Sherman Oaks|Van Nuys|"
            "Woodland Hills|Thousand Oaks"
        },
    ),
    "dfw": (
        "Dallas–Fort Worth",
        {
            "US-TX": "Dallas|Fort Worth|Plano|Irving|Frisco|Richardson|Allen|Carrollton|"
            "Grapevine|Addison|Arlington|McKinney|Lewisville|Denton|Coppell|Southlake|Westlake"
        },
    ),
    "austin": ("Austin area", {"US-TX": "Austin|Round Rock|Cedar Park|Pflugerville|Georgetown"}),
    "denver": (
        "Denver–Boulder",
        {
            "US-CO": "Denver|Boulder|Broomfield|Louisville|Westminster|Littleton|Englewood|"
            "Aurora|Lakewood|Golden|Centennial|Greenwood Village|Lone Tree|Longmont"
        },
    ),
    "triangle": (
        "Research Triangle",
        {
            "US-NC": "Raleigh|Durham|Chapel Hill|Cary|Morrisville|Research Triangle Park|Apex|"
            "Wake Forest"
        },
    ),
    "atlanta": (
        "Atlanta area",
        {
            "US-GA": "Atlanta|Alpharetta|Sandy Springs|Marietta|Duluth|Norcross|Kennesaw|"
            "Roswell|Dunwoody"
        },
    ),
    "chicago": (
        "Chicago area",
        {
            "US-IL": "Chicago|Evanston|Oak Brook|Schaumburg|Naperville|Rosemont|Deerfield|"
            "Northbrook|Itasca|Downers Grove"
        },
    ),
}
_METRO_OF: Final = {
    (state, city.casefold()): slug
    for slug, (_, members) in _METROS.items()
    for state, cities in members.items()
    for city in cities.split("|")
}

_COUNTY = re.compile(r"\s+county$", re.IGNORECASE)
_CITY_KEY = re.compile(r"(?P<country>[A-Za-z]{2})(?:-(?P<state>[A-Za-z]{2}))?(?:/(?P<city>.*))?")


def _city_name(part: str) -> str | None:
    """The name a city part goes by: "SF Office" → "San Francisco", "Greater Seattle Area" →
    "Seattle", "San Francisco County" → "San Francisco"."""
    text = _COUNTY.sub("", trim_noise(part)).strip()
    if not text:
        return None
    if text.casefold() in _CITY_NAMES:
        return _CITY_NAMES[text.casefold()]
    return text.title() if text.isupper() or text.islower() else text


def _known(name: str, country: str) -> bool:
    key = name.casefold()
    state = _CITY_STATES.get(key)
    return (state or "").startswith(f"{country}-") or city_country(name) == country


def _city_spot(part: str, country: str, state: str | None, *, explicit: bool) -> Spot | None:
    """The spot a city part names; unknown cities only with ``explicit`` context (a state or
    country next to them). "US-San Francisco" and "NYC-Privy" read their known piece."""
    if (area := _AREAS.get(collapse_whitespace(part).casefold())) is not None or (
        area := _AREAS.get(trim_noise(part).casefold())
    ) is not None:
        area_state = state or area[0]
        return Spot(area_state, None, area[1]) if area_state or area[1] else None
    name = _city_name(part)
    if name is None:
        return None
    if not _known(name, country):
        pieces = [
            found
            for piece in split_pieces(part)
            if piece != part
            and not names_country_or_region(piece)
            and (found := _city_name(piece)) is not None
            and _known(found, country)
        ]
        if len(pieces) == 1:
            name = pieces[0]
        elif not explicit:
            return None
    if state is None:
        table_state = _CITY_STATES.get(name.casefold())
        state = table_state if table_state and table_state.startswith(f"{country}-") else None
    return Spot(state, name, _METRO_OF.get((state or "", name.casefold())))


def _state_part(part: str) -> tuple[str, str | None] | None:
    """A state ending a part: "Texas" → ("US-TX", None); "Dallas TX" → ("US-TX", "Dallas")."""
    if (key := subdivision_key(part)) is not None:
        return key, None
    head, _, tail = part.rpartition(" ")
    if head and len(tail) == 2 and (key := subdivision_key(tail)) is not None:
        return key, head
    *before, last = split_pieces(part) or [part]
    if (  # "US-CA"
        before
        and all(map(names_country_or_region, before))
        and (key := subdivision_key(last)) is not None
    ):
        return key, None
    return None


def _spots(label: str, country: str) -> tuple[Spot, ...]:
    state: str | None
    whole = collapse_whitespace(label).casefold()
    if whole in _AREAS:
        state, metro = _AREAS[whole]
        return (Spot(state, None, metro),)
    state = None
    parts = label_parts(label)
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].casefold() in _AREAS:
            continue
        if (found := _state_part(parts[index])) is not None:
            state, head = found
            parts = [*parts[:index], *([head] if head else []), *parts[index + 1 :]]
            break
    if state is not None and not state.startswith(f"{country}-"):
        return ()
    explicit = state is not None or (len(parts) > 1 and country_code(parts[-1]) == country)
    candidates = [
        part
        for part in parts
        if part.casefold() in _AREAS
        or trim_noise(part).casefold() in _AREAS
        or not names_country_or_region(part)
    ]
    spots: list[Spot] = []
    for part in candidates:
        spot = _city_spot(part, country, state, explicit=explicit)
        if spot is not None and spot not in spots:
            spots.append(spot)
            if explicit:
                break
    if not spots and state is not None:
        spots.append(Spot(state))
    return tuple(spots)


@lru_cache(maxsize=16384)
def label_place(label: str) -> Place:
    """The label's countries, and the spots below them when it names one country and isn't
    remote."""
    countries = label_countries(label)
    if countries is None or countries == ANYWHERE or len(countries) != 1 or is_remote_label(label):
        return Place(countries)
    (country,) = countries
    return Place(countries, _spots(label, country))


def _spot_keys(country: str, spot: Spot) -> list[str]:
    keys: list[str] = []
    if spot.state:
        keys.append(spot.state)
    if spot.metro:
        keys.append(METRO_PREFIX + spot.metro)
    if spot.city:
        keys.append(f"{spot.state or country}/{spot.city}")
    return keys


@lru_cache(maxsize=16384)
def _label_keys(label: str) -> frozenset[str]:
    """Every key below the country a label belongs to, casefolded."""
    place = label_place(label)
    if not place.spots or place.countries is None:
        return frozenset()
    (country,) = place.countries
    return frozenset(key.casefold() for spot in place.spots for key in _spot_keys(country, spot))


def parse_place_key(value: str) -> str | None:
    """A place key from a URL, tidied ("us-tx" → "US-TX"); ``None`` for anything else."""
    text = value.strip()
    if text.startswith(METRO_PREFIX):
        return text if text[len(METRO_PREFIX) :] in _METROS else None
    match = _CITY_KEY.fullmatch(text)
    if match is None or match["country"].upper() not in ISO_CODES:
        return None
    key = match["country"].upper()
    if match["state"]:
        key = f"{key}-{match['state'].upper()}"
        if subdivision_name(key) is None:
            return None
    if match["city"] is not None:
        city = collapse_whitespace(match["city"])
        if not city or len(city) > MAX_CITY_LENGTH:
            return None
        key = f"{key}/{city}"
    return key


def place_label(key: str) -> str:
    """ "California", "Ontario (Canada)", "Austin, TX", "London, United Kingdom"."""
    if key.startswith(METRO_PREFIX):
        slug = key[len(METRO_PREFIX) :]
        return _METROS[slug][0] if slug in _METROS else slug
    where, _, city = key.partition("/")
    country, _, state = where.partition("-")
    if city:
        return f"{city}, {state}" if state else f"{city}, {country_name(country)}"
    if state:
        name = subdivision_name(where) or where
        return name if country == "US" else f"{name} ({country_name(country)})"
    return country_name(country)


def _in_country(labels: Sequence[str], code: str) -> bool:
    for label in labels:
        countries = label_place(label).countries
        if countries is not None and (countries == ANYWHERE or code in countries):
            return True
    return False


def posting_in_place(labels: Sequence[str], key: str) -> bool:
    """Whether any of a posting's labels is in the place. A country also takes worldwide
    remote labels, as ``filters.locations.countries`` does; remote labels are never in a
    state, metro area or city."""
    if "-" not in key and "/" not in key and ":" not in key:
        return _in_country(labels, key)
    wanted = key.casefold()
    return any(wanted in _label_keys(label) for label in labels)


def place_options(
    postings: Iterable[Sequence[str]], *, selected: str | None
) -> tuple[PlaceGroup, ...]:
    """The places these postings are in, grouped, each with how many postings it would show.
    ``selected`` stays listed even when no posting is in it."""
    counts: Counter[str] = Counter()
    names: dict[str, str] = {}  # casefolded key → key as first written
    listed: set[str] = set()
    reach: list[tuple[bool, set[str]]] = []
    for labels in postings:
        anywhere = False
        covered: set[str] = set()
        keys: set[str] = set()
        for label in labels:
            place = label_place(label)
            if place.countries == ANYWHERE:
                anywhere = True
            elif place.countries:
                covered |= place.countries
                if len(place.countries) <= MAX_LISTED_COUNTRIES:
                    listed |= place.countries
            if place.spots and place.countries:
                (country,) = place.countries
                for spot in place.spots:
                    for key in _spot_keys(country, spot):
                        keys.add(names.setdefault(key.casefold(), key))
        counts.update(keys)
        reach.append((anywhere, covered))
    for code in listed:
        counts[code] = sum(1 for anywhere, covered in reach if anywhere or code in covered)
    if selected and selected.casefold() not in names and selected not in counts:
        counts[selected] = 0

    groups: dict[str, list[PlaceOption]] = {
        "Countries": [],
        "States and provinces": [],
        "Metro areas": [],
        "Cities": [],
    }
    for key, count in counts.items():
        option = PlaceOption(key, place_label(key), count)
        if key.startswith(METRO_PREFIX):
            groups["Metro areas"].append(option)
        elif "/" in key:
            groups["Cities"].append(option)
        elif "-" in key:
            groups["States and provinces"].append(option)
        else:
            groups["Countries"].append(option)
    return tuple(
        PlaceGroup(label, tuple(sorted(options, key=lambda option: option.label.casefold())))
        for label, options in groups.items()
        if options
    )


def place_country(key: str) -> str | None:
    """The country a place key is in: "US-TX/Austin" → "US"; a metro area's from its table."""
    if key.startswith(METRO_PREFIX):
        metro = _METROS.get(key[len(METRO_PREFIX) :])
        if metro is None:
            return None
        return next(iter(metro[1])).partition("-")[0]
    return key.partition("/")[0].partition("-")[0] or None


def metro_options() -> tuple[tuple[str, str], ...]:
    """Every metro area as (key, label), in table order."""
    return tuple((METRO_PREFIX + slug, label) for slug, (label, _) in _METROS.items())
