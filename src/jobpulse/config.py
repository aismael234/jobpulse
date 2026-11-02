"""Configuration, all of it from the dashboard: preferences are the database profile
(``Profile``, edited by the setup wizard and Settings), and how the worker runs
(``InfraConfig``) is fixed defaults plus the profile's Settings → Sources & schedule
(``infra_for``). Secrets are saved on Settings too (``credentials``). There is no config
file and no ``.env``.

Every problem is reported as a ``ConfigError`` (or, for the profile, a ``ProfileError``)
that names a dotted field path such as ``filters.title.include[3].pattern``, so a typo is
quick to find.
"""

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import ErrorDetails, PydanticCustomError

from jobpulse.core import filters as core_filters
from jobpulse.core.assessment import Level
from jobpulse.core.countries import country_code
from jobpulse.core.dedupe import normalize_company
from jobpulse.core.locations import DEFAULT_SHORT_LABELS, merge_aliases
from jobpulse.core.places import parse_place_key
from jobpulse.core.profile_builder import SECTIONS, Answers
from jobpulse.core.roles import FAMILIES_BY_KEY
from jobpulse.core.scoring import Weights
from jobpulse.core.skills import SKILLS_BY_KEY
from jobpulse.data import agency_names

DEFAULT_MODELS_DIR = Path("data/models")
SIMPLIFY_NEW_GRAD_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/"
    "listings.json"
)
# The seed installs sync from once the repository is public (M7, `seed.remote`).
DEFAULT_SEED_URL = (
    "https://raw.githubusercontent.com/aismael234/jobpulse/main/src/jobpulse/data/seed/boards.json"
)
PLACEHOLDER = "<SET ME>"
EXAMPLE_CONTACT_EMAIL = "you@example.com"

NTFY_TOPIC_PATTERN = re.compile(r"[A-Za-z0-9_-]{24,64}")
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
SLUG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
BOARD_REF_PATTERN = re.compile(
    r"(greenhouse|lever|ashby)/[A-Za-z0-9][A-Za-z0-9._-]*"
    r"|workday/[A-Za-z0-9][A-Za-z0-9-]*\.wd\d{1,3}/[A-Za-z0-9][A-Za-z0-9._-]*",
    re.IGNORECASE,
)
# LinkedIn reads these search locations as anywhere in the world.
WORLDWIDE_SEARCH_LOCATION = re.compile(r"remote|anywhere|worldwide", re.IGNORECASE)
MAX_KEYWORDS = 20
MAX_KEYWORD_CHARS = 100

DEFAULT_HOST_LIMITS = {
    "boards-api.greenhouse.io": 4,
    "api.lever.co": 4,
    "api.eu.lever.co": 4,
    "api.ashbyhq.com": 4,
    "raw.githubusercontent.com": 1,
    "www.linkedin.com": 1,
}
DEFAULT_CADENCES: dict[str, dict[str, float]] = {
    "watchlist": {"every_minutes": 20, "jitter_seconds": 120},
    "simplify": {"every_minutes": 20, "jitter_seconds": 120},
    "job_boards": {"every_minutes": 30, "jitter_seconds": 900},
}

AtsType = Literal["greenhouse", "lever", "ashby", "workday"]
Region = Literal["us", "eu"]
NotifyChannel = Literal["ntfy", "discord"]
SearchSite = Literal["indeed", "linkedin"]
LlmEffort = Literal["low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True)
class FieldError:
    field: str
    message: str


class ConfigError(Exception):
    """Missing or invalid settings. ``str()`` lists every problem with its field."""

    def __init__(self, errors: list[FieldError], heading: str = "invalid settings") -> None:
        super().__init__(errors)
        self.errors = errors
        self.heading = heading

    def __str__(self) -> str:
        lines = [self.heading + ":"]
        for error in self.errors:
            lines.append(
                f"  - {error.field}: {error.message}" if error.field else f"  - {error.message}"
            )
        return "\n".join(lines)


def _check_regex(pattern: str) -> str:
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        # The detail goes in through the context so braces in the pattern aren't templated.
        raise PydanticCustomError(
            "regex", "invalid regular expression {detail}", {"detail": f"{pattern!r}: {exc}"}
        ) from None
    return pattern


def _check_board_ref(value: str) -> str:
    if not BOARD_REF_PATTERN.fullmatch(value.strip()):
        raise ValueError(
            f"{value!r} is not ats/token, e.g. greenhouse/stripe or workday/nvidia.wd5/External "
            "(ats is greenhouse, lever, ashby or workday)"
        )
    return value.strip()


def clean_keywords(values: Iterable[str]) -> list[str]:
    """Workday search keywords: whitespace collapsed, blanks and repeats (in any case) dropped.
    Raises ``ValueError`` for a keyword over ``MAX_KEYWORD_CHARS`` or more than
    ``MAX_KEYWORDS`` of them."""
    keywords: list[str] = []
    seen: set[str] = set()
    for value in values:
        keyword = " ".join(value.split())
        if not keyword or keyword.casefold() in seen:
            continue
        if len(keyword) > MAX_KEYWORD_CHARS:
            raise ValueError(f"keywords must be at most {MAX_KEYWORD_CHARS} characters each")
        seen.add(keyword.casefold())
        keywords.append(keyword)
    if len(keywords) > MAX_KEYWORDS:
        raise ValueError(f"at most {MAX_KEYWORDS} keywords")
    return keywords


def _check_company_name(value: str) -> str:
    if not normalize_company(value):
        raise ValueError(f"{value!r} is empty once case, punctuation and Inc/LLC/Ltd are dropped")
    return value


def _to_place_key(value: str) -> str:
    key = parse_place_key(value)
    if key is None or ("-" not in key and "/" not in key and ":" not in key):
        raise ValueError(
            f"{value!r} is not a place key below a country, such as US-TX, metro:bay-area or "
            "US-TX/Austin (countries go in countries)"
        )
    return key


def _to_country_code(value: str) -> str:
    code = country_code(value)
    if code is None:
        raise ValueError(
            f"{value!r} is not a country; use an ISO 3166 code such as US, CA or GB, or its "
            "English name"
        )
    return code


Regex = Annotated[str, AfterValidator(_check_regex)]
Slug = Annotated[str, Field(min_length=1, max_length=200, pattern=SLUG_PATTERN)]
BoardRef = Annotated[str, AfterValidator(_check_board_ref)]
CompanyName = Annotated[str, AfterValidator(_check_company_name)]
CountryCode = Annotated[str, AfterValidator(_to_country_code)]
PlaceKey = Annotated[str, AfterValidator(_to_place_key)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WeightedPattern(_Model):
    pattern: Regex
    weight: float = Field(default=1.0, ge=0, le=1)


class HttpConfig(_Model):
    timeout_seconds: float = Field(default=20.0, gt=0)
    max_attempts: int = Field(default=4, ge=1, le=10)
    backoff_base_seconds: float = Field(default=1.0, gt=0)
    backoff_max_seconds: float = Field(default=60.0, gt=0)
    max_retry_after_seconds: float = Field(default=120.0, gt=0)
    default_host_limit: int = Field(default=2, ge=1)
    host_limits: dict[str, Annotated[int, Field(ge=1)]] = Field(
        default_factory=lambda: dict(DEFAULT_HOST_LIMITS)
    )

    @field_validator("host_limits")
    @classmethod
    def _merge_with_defaults(cls, value: dict[str, int]) -> dict[str, int]:
        return {**DEFAULT_HOST_LIMITS, **{host.lower(): limit for host, limit in value.items()}}


class Cadence(_Model):
    every_minutes: float = Field(gt=0)
    jitter_seconds: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def _jitter_shorter_than_interval(self) -> Self:
        if self.jitter_seconds >= self.every_minutes * 60:
            raise ValueError("jitter_seconds must be shorter than the interval (every_minutes)")
        return self


class SchedulesConfig(_Model):
    watchlist: Cadence = Cadence(**DEFAULT_CADENCES["watchlist"])
    simplify: Cadence = Cadence(**DEFAULT_CADENCES["simplify"])
    job_boards: Cadence = Cadence(**DEFAULT_CADENCES["job_boards"])

    @model_validator(mode="before")
    @classmethod
    def _fill_partial_cadences(cls, data: Any) -> Any:
        # `watchlist: {every_minutes: 10}` keeps the default jitter instead of dropping to zero.
        if isinstance(data, dict):
            data = dict(data)
            for name, defaults in DEFAULT_CADENCES.items():
                if isinstance(data.get(name), dict):
                    data[name] = {**defaults, **data[name]}
        return data


class DetectionConfig(_Model):
    freshness_hours: float = Field(default=48, gt=0)
    closure_missing_polls: int = Field(default=2, ge=1)
    fingerprint_window_days: float = Field(default=30, gt=0)


class TitleRules(_Model):
    include: list[WeightedPattern] = []
    exclude: list[Regex] = []

    @field_validator("include", mode="before")
    @classmethod
    def _plain_strings_become_patterns(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [{"pattern": item} if isinstance(item, str) else item for item in value]
        return value


class LevelRules(_Model):
    include: list[Regex] = []
    exclude: list[Regex] = []
    include_bonus: float = Field(default=0.2, ge=0, le=1)


class DescriptionRules(_Model):
    max_years_experience: int | None = Field(default=None, ge=0, le=40)
    years_mode: Literal["penalize", "exclude"] = "penalize"
    level_phrases_exclude: list[Regex] = []
    stack_keywords: dict[Regex, Annotated[float, Field(ge=0)]] = {}
    stack_saturation: float = Field(default=2.0, gt=0)


class LocationRules(_Model):
    include: list[Regex] = []
    exclude: list[Regex] = []
    countries: list[CountryCode] = []  # ISO codes; a posting needs a location in one of them
    places: list[PlaceKey] = []  # when set, a posting needs a location in one of them
    remote_ok: bool = True  # with places: a remote location passes too
    aliases: dict[str, str] = {}
    unknown_location_passes: bool = True

    @field_validator("countries", "places")
    @classmethod
    def _drop_repeats(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class PayRules(_Model):
    # Postings listing US-dollar pay whose top is under this are filtered; unlisted pay passes.
    min_yearly_usd: int | None = Field(default=None, ge=0, le=10_000_000)


class FiltersConfig(_Model):
    title: TitleRules = TitleRules()
    level: LevelRules = LevelRules()
    description: DescriptionRules = DescriptionRules()
    locations: LocationRules = LocationRules()
    pay: PayRules = PayRules()


class ScoringWeights(_Model):
    freshness: float = Field(default=0.25, ge=0)
    company_priority: float = Field(default=0.20, ge=0)
    title: float = Field(default=0.30, ge=0)
    stack: float = Field(default=0.25, ge=0)
    level_mismatch: float = Field(default=0.50, ge=0)
    applicants: float = Field(default=0.10, ge=0)
    resume: float = Field(default=0.25, ge=0)  # replaces stack when a resume match is known
    llm: float = Field(default=0.15, ge=0, le=1)


class ScoringConfig(_Model):
    weights: ScoringWeights = ScoringWeights()
    priority_cap: int = Field(default=10, ge=1)
    applicant_cap: int = Field(default=200, ge=1)
    instant_threshold: float = 0.55  # the LLM layer double-checks scores within llm.band of it
    dream_companies: list[CompanyName] = []  # their postings get the top company priority


class NtfyConfig(_Model):
    base_url: str = "https://ntfy.sh"
    priority: int = Field(default=4, ge=1, le=5)
    tags: list[str] = ["briefcase"]

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("must start with https:// (or http:// for a local server)")
        return value.rstrip("/")


class DiscordConfig(_Model):
    username: str = "jobpulse"


class SavedFilterPushes(_Model):
    """Limits on one-message-per-posting pushes, which start once a saved filter exists.
    ntfy.sh allows a burst of 60, then one request per 5 s, and 250 messages a day."""

    max_per_cycle: int = Field(default=10, ge=1, le=60)
    max_per_day: int = Field(default=200, ge=1)
    spacing_seconds: float = Field(default=5.0, ge=0)


class NotifyConfig(_Model):
    channels: list[NotifyChannel] = ["ntfy"]
    ntfy: NtfyConfig = NtfyConfig()
    discord: DiscordConfig = DiscordConfig()
    saved_filters: SavedFilterPushes = SavedFilterPushes()


class SearchEntry(_Model):
    name: Slug
    site: SearchSite
    search_term: str = Field(min_length=1)
    location: str | None = None
    hours_old: int = Field(default=3, ge=1, le=168)
    results_wanted: int = Field(default=30, ge=1, le=500)
    is_remote: bool | None = None
    country_indeed: str = "USA"
    linkedin_company_ids: list[int] = []
    active: bool = True


class LinkedInPacing(_Model):
    delay_min_seconds: float = Field(default=15.0, ge=0)
    delay_max_seconds: float = Field(default=30.0, ge=0)
    cooldown_minutes: float = Field(default=30.0, ge=0)
    job_pages: bool = True
    max_job_pages_per_hour: int = Field(default=40, ge=1, le=1000)
    # How long a posting whose job page couldn't be read waits for a later cycle to read it
    # before it's judged without the page. 0 judges it at once.
    job_page_wait_hours: float = Field(default=12, ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.delay_min_seconds > self.delay_max_seconds:
            raise ValueError("delay_min_seconds must not exceed delay_max_seconds")
        return self


class SimplifyConfig(_Model):
    enabled: bool = True
    name: Slug = "new-grad"
    url: str = SIMPLIFY_NEW_GRAD_URL

    @field_validator("url")
    @classmethod
    def _https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("must be an https:// URL")
        return value


class WorkdayConfig(_Model):
    max_pages: int = Field(default=25, ge=1, le=100)


class MaintenanceConfig(_Model):
    """A source redirected to its maintenance page is retried, then paused as a whole."""

    retries: int = Field(default=2, ge=0, le=10)
    retry_delay_seconds: float = Field(default=30, ge=0, le=600)
    cooldown_minutes: float = Field(default=30, gt=0)


class LlmConfig(_Model):
    """The LLM's second opinion. It reads the stored resume (Settings, or `jobpulse resume
    set`)."""

    enabled: bool = False
    provider: Literal["anthropic"] = "anthropic"
    model: str = Field(default="claude-opus-5-5", min_length=1)
    effort: LlmEffort = "low"
    role: str = Field(default="backend software engineer", min_length=1, max_length=100)
    band: float = Field(default=0.10, gt=0, le=1)
    wanted_levels: list[Level] = []
    max_calls_per_day: int = Field(default=25, ge=1, le=1000)


def default_models_dir() -> Path:
    """``JOBPULSE_MODELS_DIR``, else ``data/models``."""
    return Path(os.environ.get("JOBPULSE_MODELS_DIR") or DEFAULT_MODELS_DIR)


class EmbeddingsConfig(_Model):
    """The local embedding model behind resume similarity (M5b)."""

    enabled: bool = True  # false on small hosts: the model takes about 200 MB of memory
    # Where `jobpulse embeddings download` puts it; JOBPULSE_MODELS_DIR moves the default (the
    # Docker image bakes the model outside the data volume).
    cache_dir: Path = Field(default_factory=default_models_dir)


class DiscoveryConfig(_Model):
    enabled: bool = True
    require_match: bool = True
    max_board_jobs: int | None = Field(default=1000, ge=1)
    max_auto_companies: int | None = Field(default=1500, ge=0)
    max_validations_per_cycle: int = Field(default=20, ge=0, le=100)
    max_page_fetches_per_cycle: int = Field(default=5, ge=0, le=50)


class SeedConfig(_Model):
    """The public seed: once a day, a watchlist cycle fetches it and adds its new boards."""

    remote: bool = False  # off until the repository is public
    url: str = DEFAULT_SEED_URL
    max_bytes: int = Field(default=2_000_000, ge=1, le=20_000_000)
    max_new_boards: int = Field(default=100, ge=1, le=1000)  # validated per sync; more wait

    @field_validator("url")
    @classmethod
    def _https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("must be an https:// URL")
        return value


class BlocklistConfig(_Model):
    companies: list[CompanyName] = []
    boards: list[BoardRef] = []


class HealthConfig(_Model):
    enabled: bool = True
    consecutive_failures: int = Field(default=3, ge=1)
    busy_min: int = Field(default=5, ge=1)
    busy_window_days: float = Field(default=7, gt=0)
    zero_hours: float = Field(default=24, gt=0)


class RetentionConfig(_Model):
    runs_days: int | None = Field(default=30, ge=1)  # None keeps every run


class SetupAnswers(_Model):
    """What the setup wizard asked (M8). The rules sections are built from these
    (``onboarding.apply_answers``), except those in ``custom``: edited in Advanced, they're
    kept as they are."""

    roles: list[str] = []  # core.roles family keys
    level: Literal["entry", "mid", "senior", "staff"] = "mid"
    max_years: int | None = Field(default=None, ge=0, le=40)  # None: the level's own
    countries: list[CountryCode] = ["US"]
    places: list[PlaceKey] = []
    remote_ok: bool = True
    min_pay: int | None = Field(default=None, ge=0, le=10_000)  # thousands of USD a year
    skills: dict[str, Literal["main", "also"]] = {}  # core.skills keys
    dream: list[CompanyName] = []
    avoid: list[CompanyName] = []
    hide_agencies: bool = True
    channel: Literal["ntfy", "discord", "none"] = "ntfy"
    job_boards: bool = True  # LinkedIn and Indeed searches
    custom: list[str] = []  # core.profile_builder.SECTIONS edited in Advanced

    def to_core(self) -> Answers:
        return Answers(
            roles=tuple(self.roles),
            level=self.level,
            max_years=self.max_years,
            countries=tuple(self.countries),
            places=tuple(self.places),
            remote_ok=self.remote_ok,
            min_pay=self.min_pay,
            skills=dict(self.skills),
            dream=tuple(self.dream),
            avoid=tuple(self.avoid),
            hide_agencies=self.hide_agencies,
            channel=self.channel,
            job_boards=self.job_boards,
        )

    @field_validator("roles")
    @classmethod
    def _known_roles(cls, value: list[str]) -> list[str]:
        unknown = [key for key in value if key not in FAMILIES_BY_KEY]
        if unknown:
            raise ValueError(f"unknown role families: {', '.join(unknown)}")
        return list(dict.fromkeys(value))

    @field_validator("skills")
    @classmethod
    def _known_skills(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = [key for key in value if key not in SKILLS_BY_KEY]
        if unknown:
            raise ValueError(f"unknown skills: {', '.join(unknown)}")
        return value

    @field_validator("custom")
    @classmethod
    def _known_sections(cls, value: list[str]) -> list[str]:
        unknown = [path for path in value if path not in SECTIONS]
        if unknown:
            raise ValueError(f"unknown sections: {', '.join(unknown)}")
        return list(dict.fromkeys(value))


PROFILE_VERSION = 1


class InfraConfig(_Model):
    """How jobpulse runs. The defaults are fixed; what Settings → Sources & schedule
    changes comes from the profile (``infra_for``). Not a file: there is no config.yaml."""

    http: HttpConfig = HttpConfig()
    schedules: SchedulesConfig = SchedulesConfig()
    detection: DetectionConfig = DetectionConfig()
    linkedin: LinkedInPacing = LinkedInPacing()
    workday: WorkdayConfig = WorkdayConfig()
    simplify: SimplifyConfig = SimplifyConfig()
    discovery: DiscoveryConfig = DiscoveryConfig()
    health: HealthConfig = HealthConfig()
    maintenance: MaintenanceConfig = MaintenanceConfig()
    retention: RetentionConfig = RetentionConfig()
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    seed: SeedConfig = SeedConfig()

    @model_validator(mode="after")
    def _job_pages_wait_within_freshness(self) -> Self:
        wait, fresh = self.linkedin.job_page_wait_hours, self.detection.freshness_hours
        if wait >= fresh:
            raise ValueError(
                f"linkedin.job_page_wait_hours ({wait:g}) must be less than "
                f"detection.freshness_hours ({fresh:g}): a posting still waiting when it stops "
                "being fresh would never be judged"
            )
        return self

    @model_validator(mode="after")
    def _run_history_covers_the_busy_window(self) -> Self:
        days, window = self.retention.runs_days, self.health.busy_window_days
        if self.health.enabled and days is not None and days < window:
            raise ValueError(
                f"retention.runs_days ({days}) must be at least health.busy_window_days "
                f"({window:g}): the busy-source health rule reads that much run history"
            )
        return self


class SourceSettings(_Model):
    """Settings → Sources & schedule: the few knobs worth turning. The worker rebuilds its
    schedule when they change (``infra_for``); everything else about how it runs is fixed."""

    check_every_minutes: int = Field(default=20, ge=5, le=240)  # company boards and the feed
    search_every_minutes: int = Field(default=30, ge=15, le=240)  # LinkedIn and Indeed
    freshness_hours: int = Field(default=48, ge=24, le=168)  # older postings are never pushed
    linkedin_pause_min_seconds: int = Field(default=15, ge=2, le=300)
    linkedin_pause_max_seconds: int = Field(default=30, ge=2, le=300)
    linkedin_job_pages: bool = True  # read each LinkedIn match's page: description, applicants
    linkedin_job_pages_per_hour: int = Field(default=40, ge=1, le=200)
    simplify_feed: bool = True  # the SimplifyJobs New-Grad list
    discovery: bool = True  # add boards linked from the feed and searches automatically
    discovery_max_companies: int = Field(default=1500, ge=0, le=10_000)
    seed_updates: bool = False  # the public seed, once a day (off until the repo is public)
    health_alerts: bool = True
    resume_matching: bool = True  # the local embedding model, about 200 MB of memory

    @model_validator(mode="after")
    def _pause_ordered(self) -> Self:
        if self.linkedin_pause_min_seconds > self.linkedin_pause_max_seconds:
            raise ValueError(
                "linkedin_pause_min_seconds must not exceed linkedin_pause_max_seconds"
            )
        return self


class Profile(_Model):
    """Your preferences: stored in the database, edited on Settings, and read by the worker
    at the start of every cycle."""

    version: int = PROFILE_VERSION
    contact_email: str | None = None  # sent in the User-Agent to the ATS APIs
    timezone: str = "UTC"
    filters: FiltersConfig = FiltersConfig()
    scoring: ScoringConfig = ScoringConfig()
    notify: NotifyConfig = NotifyConfig()
    searches: list[SearchEntry] = []
    blocklist: BlocklistConfig = BlocklistConfig()
    hide_agencies: bool = True  # block the staffing agencies in data/seed/agencies.json
    llm: LlmConfig = LlmConfig()
    sources: SourceSettings = SourceSettings()
    answers: SetupAnswers | None = None  # the setup wizard's; None: imported or hand-written

    @field_validator("contact_email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not EMAIL_PATTERN.fullmatch(value):
            raise ValueError(f"{value!r} doesn't look like an email address")
        if not value.isascii():
            raise ValueError("must be ASCII: it goes into the User-Agent header")
        return value

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                f"unknown IANA timezone {value!r} (e.g. America/Los_Angeles)"
            ) from None
        return value

    @field_validator("searches")
    @classmethod
    def _unique_search_names(cls, value: list[SearchEntry]) -> list[SearchEntry]:
        seen: dict[str, int] = {}
        for index, entry in enumerate(value):
            if entry.name in seen:
                raise ValueError(
                    f"searches[{index}] is a duplicate name of searches[{seen[entry.name]}] ({entry.name})"
                )
            seen[entry.name] = index
        return value


class AppConfig(InfraConfig, Profile):
    """What the rest of jobpulse reads: how the worker runs (``infra_for``) with the profile
    (``compose``)."""


def scoring_hash(profile: Profile) -> str:
    """Names the filters and scoring that open matches were scored with: when it changes,
    the worker re-scores them. Settings left at their defaults don't count, so a new
    jobpulse that adds one doesn't re-score every open match."""
    data = profile.model_dump(mode="json", include={"filters", "scoring"}, exclude_defaults=True)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def infra_for(
    profile: Profile | None, *, data_dir: Path | None = None, base: InfraConfig | None = None
) -> InfraConfig:
    """How the worker runs for ``profile``: ``base`` (the fixed defaults) with Settings →
    Sources & schedule applied. The embedding model is looked for in ``data_dir``/models
    unless ``JOBPULSE_MODELS_DIR`` says otherwise (the Docker image bakes it elsewhere)."""
    sources = (profile or Profile()).sources
    data = (base or InfraConfig()).model_dump(include=set(InfraConfig.model_fields))
    if data_dir is not None and not os.environ.get("JOBPULSE_MODELS_DIR"):
        data["embeddings"]["cache_dir"] = data_dir / "models"
    for job, minutes in (
        ("watchlist", sources.check_every_minutes),
        ("simplify", sources.check_every_minutes),
        ("job_boards", sources.search_every_minutes),
    ):
        cadence = data["schedules"][job]
        cadence["every_minutes"] = minutes
        cadence["jitter_seconds"] = min(cadence["jitter_seconds"], minutes * 60 / 2)
    data["detection"]["freshness_hours"] = sources.freshness_hours
    data["linkedin"].update(
        delay_min_seconds=sources.linkedin_pause_min_seconds,
        delay_max_seconds=sources.linkedin_pause_max_seconds,
        job_pages=sources.linkedin_job_pages,
        max_job_pages_per_hour=sources.linkedin_job_pages_per_hour,
    )
    data["simplify"]["enabled"] = sources.simplify_feed
    data["discovery"].update(
        enabled=sources.discovery, max_auto_companies=sources.discovery_max_companies
    )
    data["seed"]["remote"] = sources.seed_updates
    data["health"]["enabled"] = sources.health_alerts
    data["embeddings"]["enabled"] = sources.resume_matching
    return InfraConfig.model_validate(data)


def compose(infra: InfraConfig, profile: Profile) -> AppConfig:
    """Both halves, already validated, as one ``AppConfig``."""
    values = {name: getattr(infra, name) for name in InfraConfig.model_fields}
    values.update({name: getattr(profile, name) for name in Profile.model_fields})
    return AppConfig.model_construct(**values)


def split(config: AppConfig) -> tuple[InfraConfig, Profile]:
    infra = InfraConfig.model_construct(
        **{name: getattr(config, name) for name in InfraConfig.model_fields}
    )
    profile = Profile.model_construct(
        **{name: getattr(config, name) for name in Profile.model_fields}
    )
    return infra, profile


class ProfileError(ValueError):
    """An invalid profile. ``str()`` lists every problem with its field path."""

    def __init__(self, errors: list[FieldError]) -> None:
        super().__init__(errors)
        self.errors = errors

    def __str__(self) -> str:
        return "; ".join(
            f"{error.field}: {error.message}" if error.field else error.message
            for error in self.errors
        )


def upgrade_profile_data(data: dict[str, Any]) -> dict[str, Any]:
    """Profile data written by an older jobpulse, brought up to ``PROFILE_VERSION``.
    Migrations never touch the profile's JSON, so changes to its shape go here."""
    version = data.get("version", PROFILE_VERSION)
    if isinstance(version, int) and version > PROFILE_VERSION:
        raise ProfileError(
            [
                FieldError(
                    "version",
                    f"written by a newer jobpulse (version {version}; this one reads up to "
                    f"{PROFILE_VERSION})",
                )
            ]
        )
    return {**data, "version": PROFILE_VERSION}


def parse_profile(data: Any) -> Profile:
    """Validate profile data (stored JSON or the Advanced editor's YAML); ``ProfileError``
    names every bad field and never echoes a value it can't show."""
    if not isinstance(data, dict):
        raise ProfileError(
            [FieldError("", "the profile must be a mapping of section names to values")]
        )
    try:
        return Profile.model_validate(upgrade_profile_data(data))
    except ValidationError as exc:
        raise ProfileError(describe_errors(exc)) from None


def parse_profile_yaml(text: str) -> Profile:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileError([FieldError("", _yaml_problem(exc))]) from None
    return parse_profile({} if data is None else data)


def profile_data(profile: Profile) -> dict[str, Any]:
    """The profile as JSON-ready data: what the database stores."""
    return profile.model_dump(mode="json")


def profile_yaml(profile: Profile) -> str:
    return yaml.safe_dump(
        profile_data(profile), sort_keys=False, allow_unicode=True, default_flow_style=False
    )


def secret_problem(name: str, value: str) -> str | None:
    """Why a secret's value can't be used (``None`` when it can); never echoes the value."""
    if name == "ntfy_topic" and not NTFY_TOPIC_PATTERN.fullmatch(value):
        return (
            "must be 24–64 characters of letters, digits, '_' or '-'; anyone who knows or "
            "guesses the topic can read your alerts"
        )
    if name == "discord_webhook_url" and not value.startswith("https://"):
        return "must be an https:// webhook URL"
    return None


def require_secrets(
    config: AppConfig, secrets: Mapping[str, str | None], *, llm: bool = False
) -> None:
    """Check the secrets saved on Settings (``credentials.SecretStore``) that the enabled
    notification channels need, and with ``llm`` the LLM layer's key when the
    layer is on (a live poll).

    Only commands that send call this; the worker starts without them and waits. Error
    messages never echo the secret values.
    """
    errors: list[FieldError] = []
    if llm and config.llm.enabled and not secrets.get("llm_api_key"):
        errors.append(
            FieldError(
                "Anthropic API key",
                "not set, but the LLM layer is on; add it on Settings → Advanced",
            )
        )
    for channel, name, label in (
        ("ntfy", "ntfy_topic", "ntfy topic"),
        ("discord", "discord_webhook_url", "Discord webhook URL"),
    ):
        if channel not in config.notify.channels:
            continue
        value = secrets.get(name)
        if not value:
            errors.append(FieldError(label, f"not set, but notifications go to {channel}"))
        elif problem := secret_problem(name, value):
            errors.append(FieldError(label, problem))
    if errors:
        raise ConfigError(errors, heading="missing or invalid secrets (set them on Settings)")


def config_warnings(config: AppConfig) -> list[str]:
    """Things that are valid but almost certainly not what you want yet."""
    warnings: list[str] = []
    if config.contact_email in (None, EXAMPLE_CONTACT_EMAIL):
        warnings.append(
            "contact_email isn't set (Settings): set yours so API operators can reach you"
        )
    pattern_lists = {
        "filters.title.include": [p.pattern for p in config.filters.title.include],
        "filters.title.exclude": config.filters.title.exclude,
        "filters.level.include": config.filters.level.include,
        "filters.level.exclude": config.filters.level.exclude,
        "filters.locations.include": config.filters.locations.include,
    }
    for field, patterns in pattern_lists.items():
        if PLACEHOLDER in patterns:
            warnings.append(
                f"{field} still contains the placeholder {PLACEHOLDER!r}; it matches nothing "
                "until you replace it"
            )
    for index, search in enumerate(config.searches):
        if search.site != "linkedin":
            continue
        if search.is_remote:
            warnings.append(
                f"searches[{index}] ({search.name}) sets is_remote, which LinkedIn ignores; "
                "put 'remote' in its search_term instead"
            )
        if search.location and WORLDWIDE_SEARCH_LOCATION.fullmatch(search.location.strip()):
            warnings.append(
                f"searches[{index}] ({search.name}) has location {search.location!r}, which "
                "LinkedIn searches worldwide; use a country such as 'United States', and "
                "filters.locations.countries to keep only the postings there"
            )
    return warnings


def _format_location(location: tuple[int | str, ...]) -> str:
    text = ""
    for part in location:
        if isinstance(part, int):
            text += f"[{part}]"
        elif part == "[key]":
            text += " (key)"
        else:
            text += f".{part}" if text else part
    return text


def describe_errors(exc: ValidationError) -> list[FieldError]:
    """Each validation error with its dotted field path, as Settings shows
    them."""
    return [FieldError(_format_location(e["loc"]), _describe(e)) for e in exc.errors()]


def _describe(error: ErrorDetails) -> str:
    kind = error["type"]
    if kind == "extra_forbidden":
        return "unknown key (check the spelling)"
    if kind == "missing":
        return "required, but missing"
    message = error["msg"].removeprefix("Value error, ")
    value = error.get("input")
    if kind not in ("regex", "value_error") and isinstance(value, str | int | float):
        message += f" (got {value!r})"
    return message


def _yaml_problem(exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or str(exc)
    if mark is not None:
        return f"YAML syntax error at line {mark.line + 1}, column {mark.column + 1}: {problem}"
    return f"YAML syntax error: {problem}"


def _patterns(patterns: list[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns if p != PLACEHOLDER)


def filter_rules(config: Profile) -> core_filters.FilterRules:
    """Compile the filter section into core rules (case-insensitive, placeholders dropped)."""
    filters = config.filters
    return core_filters.FilterRules(
        title_include=tuple(
            core_filters.WeightedRegex(re.compile(rule.pattern, re.IGNORECASE), rule.weight)
            for rule in filters.title.include
            if rule.pattern != PLACEHOLDER
        ),
        title_exclude=_patterns(filters.title.exclude),
        level_include=_patterns(filters.level.include),
        level_exclude=_patterns(filters.level.exclude),
        level_include_bonus=filters.level.include_bonus,
        max_years=filters.description.max_years_experience,
        years_mode=filters.description.years_mode,
        level_phrases_exclude=_patterns(filters.description.level_phrases_exclude),
        stack=tuple(
            core_filters.WeightedRegex(re.compile(pattern, re.IGNORECASE), weight)
            for pattern, weight in filters.description.stack_keywords.items()
            if pattern != PLACEHOLDER
        ),
        stack_saturation=filters.description.stack_saturation,
        locations=core_filters.LocationRules(
            include=_patterns(filters.locations.include),
            exclude=_patterns(filters.locations.exclude),
            countries=frozenset(filters.locations.countries),
            unknown_passes=filters.locations.unknown_location_passes,
            places=tuple(filters.locations.places),
            remote_ok=filters.locations.remote_ok,
        ),
        blocklist=company_blocklist(config),
        min_yearly_usd=filters.pay.min_yearly_usd,
        dream_companies=frozenset(
            name for name in map(normalize_company, config.scoring.dream_companies) if name
        ),
    )


def company_blocklist(config: Profile) -> core_filters.Blocklist:
    """The profile's blocklist, with the staffing agencies while ``hide_agencies`` is on."""
    agencies = agency_names() if config.hide_agencies else ()
    return core_filters.Blocklist.build(
        companies=[*config.blocklist.companies, *agencies], boards=config.blocklist.boards
    )


def scoring_weights(config: Profile) -> Weights:
    weights = config.scoring.weights
    return Weights(
        freshness=weights.freshness,
        company_priority=weights.company_priority,
        title=weights.title,
        stack=weights.stack,
        level_mismatch=weights.level_mismatch,
        applicants=weights.applicants,
        resume=weights.resume,
        llm=weights.llm,
    )


def location_aliases(config: Profile) -> dict[str, str]:
    return merge_aliases(config.filters.locations.aliases)


def short_labels(config: Profile) -> dict[str, str]:
    """Display names for push titles: built-ins plus short alias keys such as ``SF``."""
    reverse = {
        label: key for key, label in config.filters.locations.aliases.items() if len(key) <= 4
    }
    return {**DEFAULT_SHORT_LABELS, **reverse}
