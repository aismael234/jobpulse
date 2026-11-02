"""M8: the wizard's answers become profile sections, deterministically and within the caps,
and picking every role family at the mid level reproduces the first install's tuned rules."""

import re
from pathlib import Path

import pytest
import yaml

from jobpulse.config import Profile, filter_rules
from jobpulse.core.filters import check_title
from jobpulse.core.profile_builder import (
    MAX_INDEED_SEARCHES,
    MAX_LINKEDIN_SEARCHES,
    MAX_WORKDAY_KEYWORDS,
    SECTIONS,
    Answers,
    build_rules,
    searches,
    workday_keywords,
)
from jobpulse.core.roles import LEVELS, ROLE_FAMILIES, level_for_years, suggest_families
from jobpulse.core.skills import SKILLS, find_skills

GOLDEN = Path(__file__).parents[2] / "fixtures" / "profile" / "golden_rules.yaml"
EVERY_FAMILY = tuple(family.key for family in ROLE_FAMILIES)


def golden() -> dict[str, object]:
    data = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_every_family_at_mid_level_reproduces_the_tuned_rules() -> None:
    reference = golden()
    rules = build_rules(Answers(roles=EVERY_FAMILY, level="mid", max_years=5))

    assert rules["filters.title"] == reference["title"]
    assert rules["filters.level"] == reference["level"]
    description = rules["filters.description"]
    assert {
        "max_years_experience": description["max_years_experience"],
        "years_mode": description["years_mode"],
    } == reference["description"]
    assert rules["filters.locations"]["countries"] == reference["countries"]


def test_the_mid_level_alone_gives_five_years() -> None:
    rules = build_rules(Answers(roles=("backend",), level="mid"))
    assert rules["filters.description"]["max_years_experience"] == 5
    assert rules["filters.description"]["years_mode"] == "exclude"


def test_build_rules_writes_every_section_and_validates_as_a_profile() -> None:
    answers = Answers(
        roles=("backend", "frontend"),
        level="senior",
        countries=("US", "CA"),
        places=("metro:bay-area", "US-TX/Austin"),
        remote_ok=False,
        min_pay=150,
        skills={"python": "main", "react": "also"},
        dream=("Stripe",),
        avoid=("Acme Staffing",),
        hide_agencies=False,
        channel="discord",
    )
    rules = build_rules(answers)
    assert set(rules) == set(SECTIONS)

    data: dict[str, object] = {}
    for path, value in rules.items():
        *parents, leaf = path.split(".")
        node = data
        for parent in parents:
            node = node.setdefault(parent, {})  # type: ignore[assignment]
        node[leaf] = value  # type: ignore[index]
    profile = Profile.model_validate(data)

    assert profile.filters.pay.min_yearly_usd == 150_000
    assert profile.filters.locations.places == ["metro:bay-area", "US-TX/Austin"]
    assert profile.scoring.dream_companies == ["Stripe"]
    assert profile.blocklist.companies == ["Acme Staffing"]
    assert profile.notify.channels == ["discord"]
    assert profile.hide_agencies is False
    assert profile.filters.description.stack_keywords == {
        r"\bpython\b": 1.0,
        r"\breact(\.?js)?\b(?! native)": 0.5,
    }
    compiled = filter_rules(profile)
    assert compiled.is_dream("Stripe, Inc.")
    assert compiled.min_yearly_usd == 150_000


def test_build_rules_is_deterministic() -> None:
    answers = Answers(roles=("software", "ml_ai", "data_engineering"), places=("US-NY",))
    assert build_rules(answers) == build_rules(answers)


def test_title_weights_scale_so_the_strongest_family_weighs_one() -> None:
    rules = build_rules(Answers(roles=("frontend", "mobile")))
    weights = [rule["weight"] for rule in rules["filters.title"]["include"]]
    assert weights == [1.0, 0.75]


def test_searches_respect_the_caps_and_have_stable_names() -> None:
    answers = Answers(roles=EVERY_FAMILY, countries=("US",), places=("metro:bay-area", "metro:nyc"))
    found = searches(answers)
    linkedin = [entry for entry in found if entry["site"] == "linkedin"]
    indeed = [entry for entry in found if entry["site"] == "indeed"]
    assert len(linkedin) == MAX_LINKEDIN_SEARCHES
    assert len(indeed) == MAX_INDEED_SEARCHES
    assert linkedin[0] == {
        "name": "machine-learning-engineer-san-francisco-bay-area-linkedin",
        "site": "linkedin",
        "search_term": "machine learning engineer",
        "location": "San Francisco Bay Area",
        "hours_old": 3,
        "results_wanted": 25,
    }
    assert indeed[0]["country_indeed"] == "USA"
    names = [entry["name"] for entry in found]
    assert len(set(names)) == len(names)
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) for name in names)


def test_with_places_and_no_remote_the_country_is_not_searched() -> None:
    found = searches(Answers(roles=("backend",), places=("US-TX/Austin",), remote_ok=False))
    assert {entry["location"] for entry in found} == {"Austin, TX"}
    with_remote = searches(Answers(roles=("backend",), places=("US-TX/Austin",)))
    assert [entry["location"] for entry in with_remote if entry["site"] == "linkedin"] == [
        "Austin, TX",
        "United States",
    ]


def test_workday_keywords_come_from_the_strongest_families_capped() -> None:
    assert workday_keywords(Answers(roles=EVERY_FAMILY)) == (
        "machine learning engineer",
        "AI engineer",
        "computer vision",
        "forward deployed",
        "software engineer",
    )
    assert len(workday_keywords(Answers(roles=EVERY_FAMILY))) == MAX_WORKDAY_KEYWORDS
    assert workday_keywords(Answers(roles=("backend", "full_stack"))) == (
        "backend engineer",
        "full stack",
    )
    assert workday_keywords(Answers()) == ()


def test_the_catalogs_compile() -> None:
    for family in ROLE_FAMILIES:
        for rule in family.titles:
            re.compile(rule.pattern, re.IGNORECASE)
    for level in LEVELS:
        for pattern in (*level.include, *level.exclude):
            re.compile(pattern, re.IGNORECASE)
    keys = [skill.key for skill in SKILLS]
    assert len(keys) == len(set(keys))
    assert len(SKILLS) >= 150
    for skill in SKILLS:
        re.compile(skill.pattern, re.IGNORECASE)


@pytest.mark.parametrize(
    ("title", "family"),
    [
        ("Senior Software Engineer", "software"),
        ("Backend Engineer II", "backend"),
        ("ML Engineer", "ml_ai"),
        ("Site Reliability Engineer", "devops_sre"),
        ("Data Engineer", "data_engineering"),
        ("iOS Mobile Engineer", "mobile"),
    ],
)
def test_resume_titles_suggest_families(title: str, family: str) -> None:
    assert family in suggest_families([title])


def test_platform_on_a_resume_suggests_nothing() -> None:
    assert suggest_families(["Platform team, Acme"]) == []


@pytest.mark.parametrize(
    ("years", "level"), [(None, "entry"), (1, "entry"), (3, "mid"), (7, "senior"), (12, "staff")]
)
def test_level_for_years(years: int | None, level: str) -> None:
    assert level_for_years(years) == level


@pytest.mark.parametrize(
    ("text", "found", "absent"),
    [
        (
            "Built services in Python and Go (golang) on AWS with Docker.",
            {"python", "go", "aws", "docker"},
            {"java"},
        ),
        (
            "JavaScript and TypeScript with React and Node.js",
            {"javascript", "typescript", "react", "node-js"},
            {"java", "react-native"},
        ),
        ("Shipped a React Native app", {"react-native"}, {"react"}),
        (
            "PostgreSQL, pgvector, RAG pipelines with LangChain",
            {"postgresql", "pgvector", "rag", "langchain"},
            set(),
        ),
        ("Wrote C++ and C# tools", {"cplusplus", "csharp"}, {"c"}),
    ],
)
def test_skills_found_in_text(text: str, found: set[str], absent: set[str]) -> None:
    keys = set(find_skills(text))
    assert found <= keys
    assert not keys & absent


def test_every_family_title_matches_its_own_example() -> None:
    rules = filter_rules(
        Profile.model_validate(
            {"filters": {"title": build_rules(Answers(roles=EVERY_FAMILY))["filters.title"]}}
        )
    )
    for title in ("Software Engineer", "Backend Developer", "Founding Engineer", "Data Engineer"):
        assert check_title(title, rules).passed


def test_terms_and_places_interleave_under_the_cap() -> None:
    answers = Answers(
        roles=("software", "backend", "full_stack"), places=("metro:bay-area", "US-TX")
    )
    linkedin = [
        (entry["search_term"], entry["location"])
        for entry in searches(answers)
        if entry["site"] == "linkedin"
    ]
    assert linkedin == [
        ("software engineer", "San Francisco Bay Area"),
        ("backend engineer", "San Francisco Bay Area"),
        ("software engineer", "Texas"),
        ("full stack engineer", "San Francisco Bay Area"),
    ]
