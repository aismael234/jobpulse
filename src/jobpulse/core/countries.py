"""Which countries a normalized location label is in, for ``filters.locations.countries``.

"San Francisco, CA", "Sunnyvale" and "Remote (US)" are the US; "Bangalore, Karnataka, India"
and "Pune" are India; "Remote (EMEA)" is every EMEA country; "Remote (Global)" is anywhere.
The tables are finite on purpose: a label they can't place is unknown (``None``), and so is a
name shared by places in different countries (Georgia the state and the country, Cambridge
MA and UK), rather than guessed. Codes are ISO 3166-1 alpha-2, except that Puerto Rico
("San Juan, PR") counts as the US.
"""

import re
from collections.abc import Iterable
from typing import Final

from jobpulse.core.text import collapse_whitespace

ANYWHERE: Final = frozenset({"*"})
_US: Final = frozenset({"US"})
_CANADA: Final = frozenset({"CA"})


def _table(text: str) -> dict[str, str]:
    """``CODE Name|Other name`` lines → {casefolded name: CODE}."""
    table: dict[str, str] = {}
    for line in text.strip().splitlines():
        code, _, names = line.strip().partition(" ")
        for name in names.split("|"):
            if name:
                table[name.strip().casefold()] = code
    return table


def _codes(text: str) -> frozenset[str]:
    return frozenset(text.split())


def _first_names(text: str) -> dict[str, str]:
    """``CODE Name|Other name`` lines → {CODE: Name}; a line without a name is left out."""
    names: dict[str, str] = {}
    for line in text.strip().splitlines():
        code, _, rest = line.strip().partition(" ")
        if name := rest.split("|")[0].strip():
            names[code] = name
    return names


# English names from tzdata's iso3166.tab, with common variants. Georgia is left out (a US
# state too), and so are Puerto Rico (counted as the US) and plain "Congo" (two countries).
_COUNTRY_LINES = """
AD Andorra
AE United Arab Emirates|UAE|U.A.E.
AF Afghanistan
AG Antigua and Barbuda|Antigua & Barbuda
AI Anguilla
AL Albania
AM Armenia
AO Angola
AQ Antarctica
AR Argentina
AS American Samoa|Samoa (American)
AT Austria|Österreich
AU Australia
AW Aruba
AX Åland Islands|Aland Islands
AZ Azerbaijan
BA Bosnia and Herzegovina|Bosnia & Herzegovina|Bosnia
BB Barbados
BD Bangladesh
BE Belgium|België|Belgique
BF Burkina Faso
BG Bulgaria
BH Bahrain
BI Burundi
BJ Benin
BL Saint Barthélemy|St Barthelemy|St. Barthélemy
BM Bermuda
BN Brunei|Brunei Darussalam
BO Bolivia
BQ Caribbean Netherlands|Caribbean NL|Bonaire
BR Brazil|Brasil
BS Bahamas|The Bahamas
BT Bhutan
BV Bouvet Island
BW Botswana
BY Belarus
BZ Belize
CA Canada
CC Cocos (Keeling) Islands|Cocos Islands
CD Democratic Republic of the Congo|DR Congo|DRC|Congo (Dem. Rep.)|Congo-Kinshasa
CF Central African Republic|Central African Rep.
CG Republic of the Congo|Congo (Rep.)|Congo-Brazzaville
CH Switzerland|Schweiz|Suisse|Svizzera
CI Côte d'Ivoire|Côte d’Ivoire|Cote d'Ivoire|Ivory Coast
CK Cook Islands
CL Chile
CM Cameroon
CN China|Mainland China|People's Republic of China|PRC
CO Colombia
CR Costa Rica
CU Cuba
CV Cape Verde|Cabo Verde
CW Curaçao|Curacao
CX Christmas Island
CY Cyprus
CZ Czechia|Czech Republic
DE Germany|Deutschland
DJ Djibouti
DK Denmark|Danmark
DM Dominica
DO Dominican Republic
DZ Algeria
EC Ecuador
EE Estonia
EG Egypt
EH Western Sahara
ER Eritrea
ES Spain|España|Espana
ET Ethiopia
FI Finland|Suomi
FJ Fiji
FK Falkland Islands
FM Micronesia
FO Faroe Islands
FR France
GA Gabon
GB United Kingdom|UK|U.K.|Great Britain|Britain|Britain (UK)|England|Scotland|Wales|Northern Ireland
GD Grenada
GF French Guiana
GG Guernsey
GH Ghana
GI Gibraltar
GL Greenland
GM Gambia|The Gambia
GN Guinea
GP Guadeloupe
GQ Equatorial Guinea
GR Greece
GS South Georgia and the South Sandwich Islands|South Georgia & the South Sandwich Islands
GT Guatemala
GU Guam
GW Guinea-Bissau
GY Guyana
HK Hong Kong|Hong Kong SAR|Hong Kong SAR China
HM Heard Island and McDonald Islands|Heard Island & McDonald Islands
HN Honduras
HR Croatia
HT Haiti
HU Hungary
ID Indonesia
IE Ireland|Republic of Ireland
IL Israel
IM Isle of Man
IN India
IO British Indian Ocean Territory
IQ Iraq
IR Iran
IS Iceland
IT Italy|Italia
JE Jersey
JM Jamaica
JO Jordan
JP Japan
KE Kenya
KG Kyrgyzstan
KH Cambodia
KI Kiribati
KM Comoros
KN Saint Kitts and Nevis|St Kitts & Nevis|St. Kitts and Nevis
KP North Korea|Korea (North)
KR South Korea|Korea|Republic of Korea|Korea (South)
KW Kuwait
KY Cayman Islands
KZ Kazakhstan
LA Laos|Lao PDR
LB Lebanon
LC Saint Lucia|St Lucia|St. Lucia
LI Liechtenstein
LK Sri Lanka
LR Liberia
LS Lesotho
LT Lithuania
LU Luxembourg
LV Latvia
LY Libya
MA Morocco
MC Monaco
MD Moldova
ME Montenegro
MF Saint Martin|St Martin (French)
MG Madagascar
MH Marshall Islands
MK North Macedonia|Macedonia
ML Mali
MM Myanmar|Burma|Myanmar (Burma)
MN Mongolia
MO Macau|Macao
MP Northern Mariana Islands
MQ Martinique
MR Mauritania
MS Montserrat
MT Malta
MU Mauritius
MV Maldives
MW Malawi
MX Mexico|México
MY Malaysia
MZ Mozambique
NA Namibia
NC New Caledonia
NE Niger
NF Norfolk Island
NG Nigeria
NI Nicaragua
NL Netherlands|The Netherlands|Holland|Nederland
NO Norway|Norge
NP Nepal
NR Nauru
NU Niue
NZ New Zealand|Aotearoa
OM Oman
PA Panama
PE Peru
PF French Polynesia
PG Papua New Guinea
PH Philippines|The Philippines
PK Pakistan
PL Poland|Polska
PM Saint Pierre and Miquelon|St Pierre & Miquelon
PN Pitcairn|Pitcairn Islands
PR
PS Palestine|Palestinian Territories
PT Portugal
PW Palau
PY Paraguay
QA Qatar
RE Réunion|Reunion
RO Romania
RS Serbia
RU Russia|Russian Federation
RW Rwanda
SA Saudi Arabia|KSA
SB Solomon Islands
SC Seychelles
SD Sudan
SE Sweden|Sverige
SG Singapore
SH Saint Helena|St Helena
SI Slovenia
SJ Svalbard and Jan Mayen|Svalbard & Jan Mayen
SK Slovakia
SL Sierra Leone
SM San Marino
SN Senegal
SO Somalia
SR Suriname
SS South Sudan
ST São Tomé and Príncipe|Sao Tome and Principe|Sao Tome & Principe
SV El Salvador
SX Sint Maarten|St Maarten (Dutch)
SY Syria
SZ Eswatini|Swaziland|Eswatini (Swaziland)
TC Turks and Caicos Islands|Turks & Caicos Is
TD Chad
TF French Southern Territories|French S. Terr.
TG Togo
TH Thailand
TJ Tajikistan
TK Tokelau
TL Timor-Leste|East Timor
TM Turkmenistan
TN Tunisia
TO Tonga
TR Türkiye|Turkiye|Turkey
TT Trinidad and Tobago|Trinidad & Tobago
TV Tuvalu
TW Taiwan
TZ Tanzania
UA Ukraine
UG Uganda
UM United States Minor Outlying Islands|US minor outlying islands
US United States|United States of America|USA|U.S.A.|U.S.A|US|U.S.|U.S
UY Uruguay
UZ Uzbekistan
VA Vatican City|Holy See
VC Saint Vincent and the Grenadines|St Vincent
VE Venezuela
VG British Virgin Islands|Virgin Islands (UK)
VI US Virgin Islands|U.S. Virgin Islands|Virgin Islands (US)
VN Vietnam|Viet Nam
VU Vanuatu
WF Wallis and Futuna|Wallis & Futuna
WS Samoa|Samoa (western)
YE Yemen
YT Mayotte
ZA South Africa
ZM Zambia
ZW Zimbabwe
GE
"""
_COUNTRIES: Final = _table(_COUNTRY_LINES)
ISO_CODES: Final = frozenset(line.split()[0] for line in _COUNTRY_LINES.strip().splitlines())

_US_STATE_LINES: Final = """
    AL Alabama
    AK Alaska
    AZ Arizona
    AR Arkansas
    CA California
    CO Colorado
    CT Connecticut
    DE Delaware
    DC District of Columbia
    FL Florida
    GA Georgia
    HI Hawaii
    ID Idaho
    IL Illinois
    IN Indiana
    IA Iowa
    KS Kansas
    KY Kentucky
    LA Louisiana
    ME Maine
    MD Maryland
    MA Massachusetts
    MI Michigan
    MN Minnesota
    MS Mississippi
    MO Missouri
    MT Montana
    NE Nebraska
    NV Nevada
    NH New Hampshire
    NJ New Jersey
    NM New Mexico
    NY New York
    NC North Carolina
    ND North Dakota
    OH Ohio
    OK Oklahoma
    OR Oregon
    PA Pennsylvania
    PR Puerto Rico
    RI Rhode Island
    SC South Carolina
    SD South Dakota
    TN Tennessee
    TX Texas
    UT Utah
    VT Vermont
    VA Virginia
    WA Washington
    WV West Virginia
    WI Wisconsin
    WY Wyoming
    """
_US_STATES: Final = _table(_US_STATE_LINES)
_US_STATE_CODES: Final = frozenset(code for code in _US_STATES.values())
_CANADIAN_PROVINCE_LINES: Final = """
    AB Alberta
    BC British Columbia
    MB Manitoba
    NB New Brunswick
    NL Newfoundland and Labrador|Newfoundland
    NS Nova Scotia
    NT Northwest Territories
    NU Nunavut
    ON Ontario
    PE Prince Edward Island|PEI
    QC Quebec|Québec
    SK Saskatchewan
    YT Yukon
    """
_CANADIAN_PROVINCES: Final = _table(_CANADIAN_PROVINCE_LINES)
_CANADIAN_PROVINCE_CODES: Final = frozenset(_CANADIAN_PROVINCES.values())

# States and provinces abroad that labels give without their country ("Pune, Maharashtra").
_SUBDIVISIONS: Final = _table(
    """
    AU New South Wales|NSW|Queensland|QLD|Western Australia|South Australia|Tasmania|Australian Capital Territory|Northern Territory
    BR São Paulo|Sao Paulo|Rio de Janeiro|Minas Gerais|Paraná|Parana|Santa Catarina|Rio Grande do Sul
    CH Zurich|Zürich|Geneva|Genève|Vaud|Basel-Stadt
    CN Guangdong|Zhejiang|Jiangsu|Sichuan|Hubei|Fujian|Shandong
    DE Bavaria|Bayern|Baden-Württemberg|Baden-Wurttemberg|Hesse|Hessen|North Rhine-Westphalia|Nordrhein-Westfalen|Lower Saxony|Niedersachsen|Saxony|Sachsen
    ES Catalonia|Cataluña|Catalunya|Community of Madrid|Comunidad de Madrid|Andalusia|Andalucía|Valencian Community|Basque Country
    FR Île-de-France|Ile-de-France|Auvergne-Rhône-Alpes|Occitanie|Nouvelle-Aquitaine|Hauts-de-France|Bretagne|Brittany
    IE County Dublin|Co. Dublin|County Cork|Co. Cork|County Galway
    IL Tel Aviv District|Haifa District|Jerusalem District
    IN Karnataka|Maharashtra|Telangana|Tamil Nadu|Haryana|Uttar Pradesh|NCT of Delhi|Delhi NCR|Gujarat|Kerala|West Bengal|Andhra Pradesh|Rajasthan|Madhya Pradesh|Odisha
    IT Lombardy|Lombardia|Lazio|Piedmont|Piemonte|Emilia-Romagna|Tuscany|Toscana|Veneto|Campania
    JP Kanagawa|Aichi
    KR Gyeonggi|Gyeonggi-do
    MX Jalisco|Nuevo León|Nuevo Leon
    MY Selangor
    NL North Holland|Noord-Holland|South Holland|Zuid-Holland|North Brabant|Noord-Brabant
    PL Masovian Voivodeship|Mazowieckie|Lesser Poland Voivodeship|Małopolskie|Lower Silesian Voivodeship|Dolnośląskie|Pomeranian Voivodeship
    """
)

# Cities that labels give alone ("London", "Sunnyvale"). A name shared with a notable city
# elsewhere is left out (Cambridge, Birmingham, Vienna VA, Naples FL, Alexandria VA, Lima OH,
# San José CR, Valencia), so it stays unknown; where job boards mean one of them almost
# always, it is that one (London, Dublin, Paris and Melbourne are abroad).
_CITIES: Final = _table(
    """
    US San Francisco|New York|New York City|NYC|Manhattan|Brooklyn|Los Angeles|Seattle|Austin|Boston|Chicago|Denver|Washington DC|Washington D.C.|San Jose|Sunnyvale|Mountain View|Palo Alto|Menlo Park|Redwood City|Santa Clara|Cupertino|San Mateo|Foster City|South San Francisco|Oakland|Berkeley|Emeryville|Fremont|Pleasanton|Milpitas|Los Gatos|Burlingame|San Bruno|Livermore|San Diego|Irvine|Santa Monica|Culver City|El Segundo|Long Beach|Pasadena|Burbank|Torrance|Hawthorne|Huntington Beach|Santa Barbara|Goleta|Carlsbad|Sacramento|Portland|Salt Lake City|Lehi|Provo|Phoenix|Scottsdale|Tempe|Chandler|Las Vegas|Dallas|Houston|San Antonio|Fort Worth|Plano|Irving|Frisco|Round Rock|Atlanta|Miami|Tampa|Orlando|Jacksonville|Fort Lauderdale|Boca Raton|Raleigh|Durham|Chapel Hill|Cary|Morrisville|Research Triangle Park|Charlotte|Nashville|Memphis|Huntsville|Pittsburgh|Philadelphia|Baltimore|Annapolis Junction|Fort Meade|Columbia|Bethesda|Reston|Herndon|McLean|Tysons|Chantilly|Arlington|Ann Arbor|Detroit|Minneapolis|St. Paul|Saint Paul|St. Louis|Saint Louis|Kansas City|Columbus|Cleveland|Cincinnati|Indianapolis|Madison|Milwaukee|Boulder|Colorado Springs|Bellevue|Redmond|Kirkland|Everett|Tacoma|Spokane|Boise|Omaha|Des Moines|Oklahoma City|Tulsa|Louisville|New Orleans|Albuquerque|Honolulu|Anchorage|Jersey City|Hoboken|Newark|Princeton|Stamford|Hartford|Providence|Somerville|Waltham|Southaven|SF|South SF|San Carlos|Daly City|Virginia Beach|Wichita|Washington D.C
    CA Toronto|Vancouver|Montreal|Montréal|Ottawa|Calgary|Edmonton|Winnipeg|Quebec City|Halifax|Waterloo|Kitchener|Mississauga|Markham|Burnaby|Sherbrooke|Longueuil|Gatineau
    GB London|Manchester|Edinburgh|Glasgow|Belfast|Cardiff|Bristol|Leeds|Liverpool|Sheffield|Nottingham|Milton Keynes|Brighton|Newcastle upon Tyne|Southampton|Guildford
    IE Dublin|Cork|Galway|Limerick
    NL Amsterdam|Rotterdam|The Hague|Eindhoven|Utrecht|Delft
    BE Brussels|Antwerp|Ghent
    CH Basel|Lausanne|Bern
    DE Berlin|Munich|München|Hamburg|Frankfurt|Cologne|Köln|Stuttgart|Düsseldorf|Dusseldorf|Leipzig|Dresden|Hannover|Nuremberg|Nürnberg|Karlsruhe|Heidelberg
    AT Wien|Graz|Linz
    CZ Prague|Praha|Brno
    PL Warsaw|Warszawa|Kraków|Krakow|Cracow|Wrocław|Wroclaw|Gdańsk|Gdansk|Poznań|Poznan|Łódź|Lodz|Katowice
    HU Budapest
    RO Bucharest|București|Cluj-Napoca|Cluj|Iași|Iasi|Timișoara|Timisoara
    BG Sofia|Plovdiv
    RS Belgrade|Novi Sad
    HR Zagreb
    SI Ljubljana
    SK Bratislava
    GR Thessaloniki
    PT Lisbon|Lisboa|Porto|Braga|Coimbra
    ES Madrid|Barcelona|Seville|Sevilla|Málaga|Malaga|Bilbao|Zaragoza|Girona|A Coruña
    IT Milan|Milano|Rome|Roma|Turin|Torino|Bologna|Firenze|Napoli|Genoa|Genova
    FR Paris|Lyon|Marseille|Toulouse|Bordeaux|Lille|Nantes|Grenoble|Montpellier|Strasbourg|Rennes|Sophia Antipolis|Courbevoie|La Défense
    DK Copenhagen|København|Aarhus
    SE Stockholm|Gothenburg|Göteborg|Malmö|Malmo|Uppsala|Lund
    NO Oslo|Bergen|Trondheim|Stavanger|Ålesund|Molde|Kristiansund
    FI Helsinki|Espoo|Tampere|Oulu
    IS Reykjavik|Reykjavík
    EE Tallinn|Tartu
    LV Riga
    LT Vilnius|Kaunas
    UA Kyiv|Kyiv City|Kiev|Lviv|Kharkiv|Odesa|Dnipro
    BY Minsk
    RU Moscow
    GE Tbilisi
    AM Yerevan
    AZ Baku
    MD Chisinau|Chișinău
    MT Valletta
    CY Limassol|Nicosia
    TR Istanbul|Ankara|Izmir
    IL Tel Aviv|Tel Aviv-Yafo|Haifa|Jerusalem|Herzliya|Ra'anana|Raanana|Petah Tikva|Netanya|Beersheba
    AE Dubai|Abu Dhabi
    SA Riyadh|Jeddah
    QA Doha
    BH Manama
    OM Muscat
    JO Amman
    LB Beirut
    EG Cairo
    NG Lagos|Abuja
    KE Nairobi
    GH Accra
    RW Kigali
    UG Kampala
    ET Addis Ababa
    TZ Dar es Salaam
    ZA Cape Town|Johannesburg|Pretoria|Durban|Port Elizabeth|Gqeberha|Bloemfontein|Polokwane|Potchefstroom|Rustenburg|Nelspruit|Mbombela|Vereeniging|Vanderbijlpark|Klerksdorp|Welkom|Witbank|eMalahleni
    MA Casablanca|Rabat
    TN Tunis
    DZ Algiers
    IN Bangalore|Bengaluru|Mumbai|Bombay|Pune|Hyderabad|Chennai|Madras|Delhi|New Delhi|Gurgaon|Gurugram|Noida|Kolkata|Calcutta|Ahmedabad|Kochi|Cochin|Jaipur|Chandigarh|Indore|Coimbatore|Thiruvananthapuram|Trivandrum|Mysore|Mysuru|Mangalore|Mangaluru|Bhubaneswar|Nagpur|Vadodara|Visakhapatnam|Lucknow|Mohali
    PK Karachi|Lahore|Islamabad|Rawalpindi
    BD Dhaka|Chittagong
    LK Colombo
    NP Kathmandu
    PH Manila|Metro Manila|Makati|Taguig|Quezon City|Pasig|Cebu|Cebu City|Davao
    ID Jakarta|Bandung|Surabaya|Yogyakarta
    MY Kuala Lumpur|Petaling Jaya|Cyberjaya|Penang|Johor Bahru
    TH Bangkok|Chiang Mai
    VN Ho Chi Minh City|Ho Chi Minh|Saigon|Hanoi|Da Nang
    KH Phnom Penh
    MM Yangon
    SG One-north
    TW Taipei|New Taipei|Hsinchu|Taichung|Kaohsiung
    JP Tokyo|Osaka|Kyoto|Yokohama|Nagoya|Fukuoka|Sapporo|Kobe
    KR Seoul|Busan|Incheon|Seongnam|Pangyo|Daejeon
    CN Beijing|Shanghai|Shenzhen|Guangzhou|Hangzhou|Chengdu|Nanjing|Wuhan|Suzhou|Xi'an|Xian|Tianjin|Chongqing|Xiamen|Qingdao|Dalian|Hefei|Changsha|Zhuhai|Dongguan|Kunshan
    MN Ulaanbaatar
    KZ Almaty|Astana
    UZ Tashkent|Samarkand|Namangan|Fergana|Andijan
    AU Sydney|Melbourne|Brisbane|Perth|Adelaide|Canberra|Gold Coast|Hobart
    NZ Auckland|Wellington|Christchurch
    MX Mexico City|Ciudad de México|Ciudad de Mexico|CDMX|Guadalajara|Monterrey|Tijuana|Querétaro|Queretaro|Puebla
    BR Rio de Janeiro|Belo Horizonte|Curitiba|Porto Alegre|Florianópolis|Florianopolis|Recife|Campinas|Brasília|Brasilia|Fortaleza|Salvador|Manaus|Belém|Belem|Goiânia|Goiania|Guarulhos|São Luís|Sao Luis|Maceió|Maceio|Natal|João Pessoa|Joao Pessoa
    AR Buenos Aires|Mendoza
    CL Santiago|Santiago de Chile|Valparaíso|Valparaiso
    CO Bogotá|Bogota|Medellín|Medellin|Barranquilla
    EC Quito|Guayaquil
    UY Montevideo
    PY Asunción|Asuncion
    VE Caracas
    GT Guatemala City
    SV San Salvador
    HN Tegucigalpa
    NI Managua
    DO Santo Domingo
    CU Havana
    """
)

_EU: Final = _codes(
    "AT BE BG CY CZ DE DK EE ES FI FR GR HR HU IE IT LT LU LV MT NL PL PT RO SE SI SK"
)
_EUROPE: Final = _EU | _codes(
    "AD AL AX BA BY CH FO GB GG GI IM IS JE LI MC MD ME MK NO RS RU SM UA VA"
)
_MIDDLE_EAST: Final = _codes("AE BH IL IQ IR JO KW LB OM PS QA SA SY TR YE")
_AFRICA: Final = _codes(
    "AO BF BI BJ BW CD CF CG CI CM CV DJ DZ EG EH ER ET GA GH GM GN GQ GW KE KM LR LS LY MA MG "
    "ML MR MU MW MZ NA NE NG RE RW SC SD SH SL SN SO SS ST SZ TD TG TN TZ UG YT ZA ZM ZW"
)
_ASIA: Final = _codes(
    "AF BD BN BT CN HK ID IN JP KG KH KP KR KZ LA LK MM MN MO MV MY NP PH PK SG TH TJ TL TM TW "
    "UZ VN"
)
_OCEANIA: Final = _codes(
    "AS AU CK FJ FM GU KI MH MP NC NF NR NU NZ PF PG PN PW SB TK TO TV VU WF WS"
)
_CENTRAL_AMERICA: Final = _codes("BZ CR GT HN NI PA SV")
_SOUTH_AMERICA: Final = _codes("AR BO BR CL CO EC FK GF GY PE PY SR UY VE")
_CARIBBEAN: Final = _codes(
    "AG AI AW BB BL BQ BS CU CW DM DO GD GP HT JM KN KY LC MF MQ MS SX TC TT VC VG VI"
)
_LATAM: Final = _codes("MX CU DO HT") | _CENTRAL_AMERICA | _SOUTH_AMERICA
_NORTH_AMERICA: Final = _codes("US CA MX")
_AMERICAS: Final = _NORTH_AMERICA | _LATAM | _CARIBBEAN | _codes("BM GL PM")

_REGIONS: Final[dict[str, frozenset[str]]] = {
    **dict.fromkeys(
        ("global", "worldwide", "world wide", "world", "anywhere", "everywhere", "international"),
        ANYWHERE,
    ),
    **dict.fromkeys(("eu", "european union"), _EU),
    "europe": _EUROPE,
    "emea": _EUROPE | _MIDDLE_EAST | _AFRICA | _codes("AM AZ GE"),
    "middle east": _MIDDLE_EAST,
    "mena": _MIDDLE_EAST | _codes("DZ EG LY MA TN"),
    "africa": _AFRICA,
    **dict.fromkeys(("apac", "asia pacific", "asia-pacific", "apj", "japac"), _ASIA | _OCEANIA),
    "asia": _ASIA,
    "oceania": _OCEANIA,
    "anz": _codes("AU NZ"),
    **dict.fromkeys(("latam", "latin america"), _LATAM),
    "south america": _SOUTH_AMERICA,
    "central america": _CENTRAL_AMERICA,
    "caribbean": _CARIBBEAN,
    **dict.fromkeys(("north america", "na"), _NORTH_AMERICA),
    **dict.fromkeys(("americas", "the americas", "amer", "amers"), _AMERICAS),
    "nordics": _codes("DK FI IS NO SE"),
    "dach": _codes("AT CH DE"),
    "benelux": _codes("BE LU NL"),
    **dict.fromkeys(
        (
            "san francisco bay area",
            "sf bay area",
            "bay area",
            "silicon valley",
            "socal",
            "southern california",
            "norcal",
            "northern california",
            "pacific northwest",
            "new england",
            "midwest",
            "east coast",
            "west coast",
            "research triangle",
            "tri-state area",
            "eastern time",
            "eastern time zone",
            "central time",
            "central time zone",
            "mountain time",
            "mountain time zone",
            "pacific time",
            "pacific time zone",
            "est",
            "edt",
            "cst",
            "cdt",
            "mst",
            "mdt",
            "pst",
            "pdt",
        ),
        _US,
    ),
}

_REMOTE = re.compile(
    r"\bremote\b|\bhome[- ]?based\b|\bwork(?:ing)? from home\b|\bwfh\b", re.IGNORECASE
)
_FILLER = re.compile(r"\b(?:in|within|the|only|based|from|ok|okay|eligible)\b", re.IGNORECASE)
_EDGE_CHARS = " -–—,:;/|()[]"
_REGION_SPLIT = re.compile(r"\s*(?:/|,|&|\+|\(|\)|\bor\b|\band\b)\s*", re.IGNORECASE)
_AND = re.compile(r"\s+and\s+", re.IGNORECASE)
_PARENTHETICAL = re.compile(r"\([^)]*\)")
_PARTS = re.compile(r"\s*(?:,|>|\s[-–—|]\s)\s*")  # "City, ST", "US > Arizona > Phoenix"
# Words around a place name: "Greater Seattle Area", "SF Office", "San Diego HQ".
_NOISE = re.compile(
    r"\b(?:greater|metropolitan|metro|metroplex|area|region|hq|headquarters|offices?|campus"
    r"|hubs?)\b",
    re.IGNORECASE,
)
_PIECES = re.compile(r"\s*[-–/]\s*")
_WORDS = re.compile(r"[\w.'’-]+")

# Countries, and whether they were named by a country, state, province or region rather
# than by a city.
type _Place = tuple[frozenset[str], bool]


def country_code(name: str) -> str | None:
    """The ISO code for a country written in config: "US", "us", "USA", "United States" →
    "US"; "UK" → "GB". Regions ("EU") and states aren't countries."""
    text = collapse_whitespace(name)
    if text.upper() in ISO_CODES:
        return text.upper()
    return _COUNTRIES.get(text.casefold())


def _union(places: Iterable[frozenset[str]]) -> frozenset[str]:
    found = list(places)
    return ANYWHERE if ANYWHERE in found else frozenset().union(*found)


def _code(token: str) -> frozenset[str] | None:
    """A US state or Canadian province code: "TX", "DC", "ON"."""
    if len(token) == 2 and token.isupper():
        if token in _US_STATE_CODES:
            return _US
        if token in _CANADIAN_PROVINCE_CODES:
            return _CANADA
    return None


def _named(key: str) -> _Place | None:
    """A casefolded name in the tables."""
    if key in _COUNTRIES:
        return frozenset({_COUNTRIES[key]}), True
    if key in _REGIONS:
        return _REGIONS[key], True
    if key in _US_STATES:
        return _US, True
    if key in _CANADIAN_PROVINCES:
        return _CANADA, True
    if key in _SUBDIVISIONS:
        return frozenset({_SUBDIVISIONS[key]}), True
    if key in _CITIES:
        return frozenset({_CITIES[key]}), False
    return None


def _place(part: str, *, several: bool) -> _Place | None:
    """One comma part of a label: a code or name in the tables, or one with words such as
    "Greater" or "Office" around it, its dash and slash pieces ("Dallas-Fort Worth",
    "US/Canada"), or a country, state or region ending it ("Dallas TX", "Northern VA")."""
    if (code := _code(part)) is not None:
        return code, True
    key = part.casefold()
    if key == "georgia":
        return (_US, True) if several else None  # "Atlanta, Georgia"; alone, maybe the country
    if (named := _named(key)) is not None:
        return named
    trimmed = collapse_whitespace(_NOISE.sub(" ", part)).strip(_EDGE_CHARS)
    if trimmed and trimmed != part:
        return _place(trimmed, several=several)
    pieces: list[_Place] = []
    for piece in _PIECES.split(part):
        if piece and piece != part and (found := _place(piece, several=True)) is not None:
            pieces.append(found)
    if pieces:
        return _union(countries for countries, _ in pieces), any(broad for _, broad in pieces)
    words = _WORDS.findall(part)
    for start in range(1, len(words)):
        tail = " ".join(words[start:])
        if (code := _code(tail)) is not None:
            return code, True
        if (named := _named(tail.casefold())) is not None and named[1]:
            return named
    return None


def _remote_countries(text: str) -> frozenset[str] | None:
    """The region of "Remote (US/Canada)" or "Home based - EMEA": every part it names."""
    region = _FILLER.sub(" ", _REMOTE.sub(" ", text))
    region = collapse_whitespace(region).strip(_EDGE_CHARS).strip()
    if not region:
        return None
    if (whole := _place(region, several=True)) is not None:
        return whole[0]
    found: list[frozenset[str]] = []
    for part in _REGION_SPLIT.split(region):
        part = part.strip(_EDGE_CHARS)
        if part and (place := _place(part, several=True)) is not None:
            found.append(place[0])
    return _union(found) if found else None


def _label_place(text: str) -> frozenset[str] | None:
    text = collapse_whitespace(_PARENTHETICAL.sub(" ", text)).strip(_EDGE_CHARS)
    parts = [part for part in (p.strip(_EDGE_CHARS) for p in _PARTS.split(text)) if part]
    known: list[_Place] = []
    for part in parts:
        if (found := _place(part, several=len(parts) > 1)) is not None:
            known.append(found)
    if not known:
        return None
    countries, broad = known[-1]
    return countries if broad else _union(found for found, _ in known)


def label_countries(label: str) -> frozenset[str] | None:
    """ISO codes of the countries a job at ``label`` can be done in: one for a place,
    several for a remote region or a list of places, ``ANYWHERE`` for worldwide remote,
    ``None`` when unknown.

    A country, state or province ending the label decides: "Paris, TX" is the US,
    "Toronto, ON" Canada and "Lousado, Braga, Portugal" Portugal. A label that ends in a
    city lists places, and is every country they are in ("SEA, SF, NY, Toronto")."""
    text = collapse_whitespace(label)
    if not text:
        return None
    if _REMOTE.search(text):
        return _remote_countries(text)
    pieces = [_label_place(piece) for piece in _AND.split(text)]
    if len(pieces) > 1 and all(piece is not None for piece in pieces):
        # "United States and Canada", but never "Trinidad and Tobago"
        return _union(piece for piece in pieces if piece is not None)
    return _label_place(text)


# --- what core/places.py reads below the country ---------------------------------------------

_COUNTRY_NAMES: Final = _first_names(_COUNTRY_LINES)
_SUBDIVISION_NAMES: Final = {
    **{f"US-{code}": name for code, name in _first_names(_US_STATE_LINES).items()},
    **{f"CA-{code}": name for code, name in _first_names(_CANADIAN_PROVINCE_LINES).items()},
}


def is_remote_label(label: str) -> bool:
    return _REMOTE.search(label) is not None


def label_parts(label: str) -> list[str]:
    """A label's parts as ``label_countries`` reads them, parentheses dropped: "Austin, TX" →
    ["Austin", "TX"]; "US > Arizona > Phoenix" → ["US", "Arizona", "Phoenix"]."""
    text = collapse_whitespace(_PARENTHETICAL.sub(" ", collapse_whitespace(label)))
    parts = (part.strip(_EDGE_CHARS) for part in _PARTS.split(text.strip(_EDGE_CHARS)))
    return [part for part in parts if part]


def subdivision_key(part: str) -> str | None:
    """ "US-TX" for "TX" or "Texas", "CA-ON" for "ON" or "Ontario"; ``None`` otherwise. Codes
    count only in capitals, so "In" or "Ok" is never a state."""
    if len(part) == 2:
        if part in _US_STATE_CODES:
            return f"US-{part}"
        return f"CA-{part}" if part in _CANADIAN_PROVINCE_CODES else None
    key = collapse_whitespace(part).casefold()
    if key in _US_STATES:
        return f"US-{_US_STATES[key]}"
    if key in _CANADIAN_PROVINCES:
        return f"CA-{_CANADIAN_PROVINCES[key]}"
    return None


def names_country_or_region(part: str) -> bool:
    """A country ("USA"), a region ("EMEA", "Bay Area") or a state abroad ("Karnataka")."""
    key = collapse_whitespace(part).casefold()
    return country_code(part) is not None or key in _REGIONS or key in _SUBDIVISIONS


def city_country(name: str) -> str | None:
    """The country of a city in the tables: "London" → "GB"."""
    return _CITIES.get(collapse_whitespace(name).casefold())


def trim_noise(part: str) -> str:
    """ "Greater Seattle Area" → "Seattle", "SF Office" → "SF"."""
    return collapse_whitespace(_NOISE.sub(" ", part)).strip(_EDGE_CHARS)


def split_pieces(part: str) -> list[str]:
    """Dash and slash pieces: "US-San Francisco" → ["US", "San Francisco"]."""
    return [piece for piece in (p.strip(_EDGE_CHARS) for p in _PIECES.split(part)) if piece]


def country_name(code: str) -> str:
    """ "US" → "United States"; the code itself when the tables have no name for it."""
    return _COUNTRY_NAMES.get(code, code)


def subdivision_name(key: str) -> str | None:
    """ "US-TX" → "Texas", "CA-ON" → "Ontario"."""
    return _SUBDIVISION_NAMES.get(key)


def subdivisions(country: str) -> tuple[tuple[str, str], ...]:
    """A country's states or provinces the tables know, as (key, name) sorted by name."""
    prefix = f"{country}-"
    return tuple(
        sorted(
            ((key, name) for key, name in _SUBDIVISION_NAMES.items() if key.startswith(prefix)),
            key=lambda item: item[1],
        )
    )
