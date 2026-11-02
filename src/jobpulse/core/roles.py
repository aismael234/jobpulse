"""Role families, the wizard's first question (M8): each with weighted title patterns, job-board
search terms, Workday keywords and a description.

The patterns and weights are the rules tuned on the first install, word for word, so picking
every family reproduces them (a golden test checks). Title excludes and level presets live
here too: ``DEFAULT_TITLE_EXCLUDES`` and ``LEVELS``.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TitleRule:
    pattern: str
    weight: float


@dataclass(frozen=True, slots=True)
class RoleFamily:
    key: str
    label: str
    description: str
    titles: tuple[TitleRule, ...]
    search_terms: tuple[str, ...] = ()  # LinkedIn and Indeed, most useful first
    workday_keywords: tuple[str, ...] = ()  # Workday's searchText, most useful first

    @property
    def weight(self) -> float:
        return max(rule.weight for rule in self.titles)


ROLE_FAMILIES: tuple[RoleFamily, ...] = (
    RoleFamily(
        "ml_ai",
        "Machine learning and AI",
        "ML engineers, AI engineers, LLM and MLOps work",
        (
            TitleRule(r"\bmachine learning\b", 1.0),
            TitleRule(r"\b(ai|ml)\b", 1.0),
            TitleRule(r"\bllm\b|\bmlops\b", 0.9),
        ),
        ("machine learning engineer", "MLOps engineer", "AI engineer"),
        ("machine learning engineer", "AI engineer"),
    ),
    RoleFamily(
        "computer_vision",
        "Computer vision",
        "Computer vision and perception engineers",
        (TitleRule(r"\bcomputer vision\b|\bperception\b", 1.0),),
        ("computer vision engineer",),
        ("computer vision",),
    ),
    RoleFamily(
        "forward_deployed",
        "Forward-deployed engineering",
        "Engineers who build with customers on site",
        (TitleRule(r"\bforward[- ]deployed\b", 0.9),),
        ("forward deployed engineer",),
        ("forward deployed",),
    ),
    RoleFamily(
        "software",
        "Software engineering",
        "Software engineers and developers, SDEs",
        (
            TitleRule(
                r"\bsoftware (engineer|developer|development|dev engineer)|\bsdes?\b|\bsde"
                r" ?(i{1,3}|[1-3])\b|\bengineer( (i{1,3}|[1-3]))?\W+software\b",
                0.9,
            ),
        ),
        ("software engineer",),
        ("software engineer",),
    ),
    RoleFamily(
        "backend",
        "Back end",
        "Back-end engineers: services, APIs, databases",
        (TitleRule(r"\bback[- ]?end\b", 0.9),),
        ("backend engineer",),
        ("backend engineer",),
    ),
    RoleFamily(
        "full_stack",
        "Full stack",
        "Full-stack engineers",
        (TitleRule(r"\bfull[- ]?stack\b", 0.9),),
        ("full stack engineer",),
        ("full stack",),
    ),
    RoleFamily(
        "frontend",
        "Front end and web",
        "Front-end, web and UI engineers",
        (TitleRule(r"\bfront[- ]?end\b|\bweb (developer|engineer)\b|\bui engineer\b", 0.8),),
        ("frontend engineer",),
        ("frontend engineer",),
    ),
    RoleFamily(
        "language",
        "Language developers",
        "Titles named after a language: Python developer, React engineer",
        (
            TitleRule(
                r"\b(python|java|javascript|typescript|react|node(\.?js)?)\s+(developer|engineer)\b",
                0.8,
            ),
        ),
    ),
    RoleFamily(
        "founding_product",
        "Founding and product engineers",
        "Founding engineers at startups, product engineers",
        (TitleRule(r"\bfounding (\w+ )?engineer\b|\bproduct engineer\b", 0.8),),
        ("founding engineer", "product engineer"),
    ),
    RoleFamily(
        "application",
        "Application developers",
        "Application developers, often in enterprise IT",
        (TitleRule(r"\bapplications? developer\b", 0.7),),
        ("application developer",),
    ),
    RoleFamily(
        "mobile",
        "Mobile",
        "iOS, Android, React Native and Flutter engineers",
        (TitleRule(r"\bmobile (developer|engineer)\b|\breact native\b|\bflutter\b", 0.6),),
        ("mobile engineer",),
        ("mobile engineer",),
    ),
    RoleFamily(
        "devops_sre",
        "DevOps and SRE",
        "DevOps, site reliability and cloud engineers",
        (TitleRule(r"\bdev(sec)?ops\b|\bsite reliability\b|\bsre\b|\bcloud engineer\b", 0.6),),
        ("devops engineer", "site reliability engineer"),
        ("devops", "site reliability"),
    ),
    RoleFamily(
        "data_engineering",
        "Data engineering",
        "Data, ETL and analytics engineers",
        (
            TitleRule(
                r"\bdata (platform |infrastructure |integration |pipelines? |warehouse )?engineer"
                r"|\betl\b|\banalytics engineer\b",
                0.6,
            ),
        ),
        ("data engineer",),
        ("data engineer",),
    ),
    RoleFamily(
        "platform_api",
        "Platform and API",
        "Any title that says platform or API",
        (TitleRule(r"\bapi\b|\bplatform\b", 0.6),),
        ("platform engineer",),
    ),
    RoleFamily(
        "solutions",
        "Solutions engineers",
        "Pre-sales and customer-facing solutions engineers",
        (TitleRule(r"\bsolutions engineer\b", 0.5),),
        ("solutions engineer",),
    ),
)
FAMILIES_BY_KEY: dict[str, RoleFamily] = {family.key: family for family in ROLE_FAMILIES}

DEFAULT_TITLE_EXCLUDES: tuple[str, ...] = (
    r"\bmanager\b",
    r"\bintern(ship)?\b",
    r"\bsales\b",
    r"\brecruit",
    r"\bdirector\b|\bhead of\b",
    r"\bresearch scientist\b|\bphd\b",
    r"\b(hardware|firmware|asic|fpga)\b",
)


@dataclass(frozen=True, slots=True)
class Level:
    key: str
    label: str
    description: str
    include: tuple[str, ...]  # a title naming the level gets a bonus
    exclude: tuple[str, ...]  # a title naming one of these is filtered
    max_years: int | None  # postings asking for more years are filtered
    years_mode: str = "exclude"  # "exclude" filters them, "penalize" lowers their score


LEVELS: tuple[Level, ...] = (
    Level(
        "entry",
        "Entry level",
        "New grad and junior roles, up to 2 years asked",
        (r"\bnew[- ]?grad\b", r"entry-level", r"\bjunior\b", r"\bjr\b"),
        (r"\bsenior\b", r"\bsr\b", r"\bstaff\b", r"\bprincipal\b", r"\blead\b"),
        2,
    ),
    Level(
        "mid",
        "Mid level",
        "Roles asking up to 5 years; senior titles stay, staff and new grad go",
        (r"entry-level", r"\bii\b", r"\bmid[- ]level\b"),
        (r"\bstaff\b", r"\bprincipal\b", r"\bnew[- ]?grad\b"),
        5,
    ),
    Level(
        "senior",
        "Senior",
        "Senior roles; postings asking for more than 10 years score lower",
        (r"\bsenior\b", r"\bsr\b"),
        (r"\bprincipal\b", r"\bjunior\b", r"\bnew[- ]?grad\b"),
        10,
        "penalize",
    ),
    Level(
        "staff",
        "Staff and above",
        "Staff, principal and lead roles",
        (r"\bstaff\b", r"\bprincipal\b", r"\blead\b"),
        (r"\bjunior\b", r"\bnew[- ]?grad\b", r"entry-level"),
        None,
        "penalize",
    ),
)
LEVELS_BY_KEY: dict[str, Level] = {level.key: level for level in LEVELS}


def level_for_years(years: int | None) -> str:
    """The level preset a resume's years of experience suggest."""
    if years is None or years < 2:
        return "entry"
    if years < 6:
        return "mid"
    if years < 10:
        return "senior"
    return "staff"


def suggest_families(titles: list[str]) -> list[str]:
    """Family keys whose title patterns match any of these lines (a resume's job titles), in
    catalog order."""
    found: list[str] = []
    for family in ROLE_FAMILIES:
        if family.key == "platform_api":  # "platform" is too common on a resume to count
            continue
        patterns = [re.compile(rule.pattern, re.IGNORECASE) for rule in family.titles]
        if any(pattern.search(title) for title in titles for pattern in patterns):
            found.append(family.key)
    return found
