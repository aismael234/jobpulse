"""Configuration: infrastructure from ``config.yaml`` and preferences from the database
profile, both validated with Pydantic; secrets from ``.env`` via pydantic-settings, or from
Settings (``credentials``).

Every problem is reported as a ``ConfigError`` (or, for the profile, a ``ProfileError``)
that names a dotted field path such as ``filters.title.include[3].pattern``, so a typo is
quick to find.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import ErrorDetails, PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict

from jobpulse.core import filters as core_filters
from jobpulse.core.assessment import Level
from jobpulse.core.atsurls import normalize_workday_token
from jobpulse.core.countries import country_code
from jobpulse.core.dedupe import normalize_company
from jobpulse.core.locations import DEFAULT_SHORT_LABELS, merge_aliases
from jobpulse.core.scoring import Weights
from jobpulse.data import agency_names

DEFAULT_CONFIG_PATH = Path("config.yaml")
DEFAULT_ENV_FILE = Path(".env")
DEFAULT_DATABASE_URL = "sqlite:///data/jobpulse.db"
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
    "watchlist": {"every_minutes": 30, "jitter_seconds": 120},
    "simplify": {"every_minutes": 30, "jitter_seconds": 120},
    "job_boards": {"every_minutes": 75, "jitter_seconds": 900},
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
    """Invalid configuration. ``str()`` lists every problem with its field path and file."""

    def __init__(
        self,
        path: Path,
        errors: list[FieldError],
        heading: str = "invalid configuration in {path}",
    ) -> None:
        super().__init__(path, errors)
        self.path = path
        self.errors = errors
        self.heading = heading

    def __str__(self) -> str:
        lines = [self.heading.format(path=self.path) + ":"]
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
    aliases: dict[str, str] = {}
    unknown_location_passes: bool = True

    @field_validator("countries")
    @classmethod
    def _drop_repeated_countries(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class FiltersConfig(_Model):
    title: TitleRules = TitleRules()
    level: LevelRules = LevelRules()
    description: DescriptionRules = DescriptionRules()
    locations: LocationRules = LocationRules()


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


class WatchlistEntry(_Model):
    name: str = Field(min_length=1)
    ats: AtsType
    token: str = Field(min_length=1, max_length=200)
    region: Region = "us"
    priority: int = Field(default=0, ge=0)
    active: bool = True
    keywords: list[str] = []  # Workday only: each one is searched (searchText)

    @field_validator("token")
    @classmethod
    def _token_fits_the_ats(cls, value: str, info: ValidationInfo) -> str:
        """A Workday board is ``tenant.wdN/site`` (its URL works too); the others are slugs."""
        value = value.strip()
        if info.data.get("ats") == "workday":
            token = normalize_workday_token(value)
            if token is None:
                raise ValueError(
                    f"{value!r} isn't a Workday board: use tenant.wdN/site, e.g. "
                    "nvidia.wd5/NVIDIAExternalCareerSite, or paste the board's URL"
                )
            return token
        if not re.fullmatch(SLUG_PATTERN, value):
            raise ValueError(
                f"{value!r} isn't a board token: letters, digits, '.', '_' and '-' only"
            )
        return value

    @field_validator("keywords")
    @classmethod
    def _keywords_for_workday(cls, value: list[str], info: ValidationInfo) -> list[str]:
        if not value:
            return value
        if info.data.get("ats") != "workday":
            raise ValueError("keywords apply to Workday boards only (ats: workday)")
        if any(not keyword.strip() for keyword in value):
            raise ValueError("keywords must not be blank")
        return clean_keywords(value)


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
    delay_min_seconds: float = Field(default=4.0, ge=0)
    delay_max_seconds: float = Field(default=8.0, ge=0)
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


class EmbeddingsConfig(_Model):
    """The local embedding model behind resume similarity (M5b)."""

    enabled: bool = True  # false on small hosts: the model takes about 200 MB of memory
    cache_dir: Path = Path("data/models")  # where `jobpulse embeddings download` puts it


class DiscoveryConfig(_Model):
    enabled: bool = True
    require_match: bool = True
    max_board_jobs: int | None = Field(default=1000, ge=1)
    max_auto_companies: int | None = Field(default=50, ge=0)
    max_validations_per_cycle: int = Field(default=10, ge=0, le=100)
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


class DashboardConfig(_Model):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)


PROFILE_VERSION = 1
# The preferences that moved from config.yaml into the database profile (M6).
PROFILE_KEYS = (
    "contact_email",
    "timezone",
    "filters",
    "scoring",
    "notify",
    "searches",
    "blocklist",
    "hide_agencies",
    "llm",
)
MOVED_KEYS = (*PROFILE_KEYS, "watchlist")


class InfraConfig(_Model):
    """config.yaml: how jobpulse runs. Every key is optional, and so is the file; your
    preferences are the database profile (``Profile``)."""

    database_url: str = DEFAULT_DATABASE_URL
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
    dashboard: DashboardConfig = DashboardConfig()
    embeddings: EmbeddingsConfig = EmbeddingsConfig()
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
    """What the rest of jobpulse reads: config.yaml's infrastructure with the profile
    (``compose``)."""


class LegacyConfig(AppConfig):
    """A config.yaml from before M6, preferences and watchlist included: what `jobpulse
    profile import` reads."""

    watchlist: list[WatchlistEntry] = []

    @field_validator("watchlist")
    @classmethod
    def _unique_companies(cls, value: list[WatchlistEntry]) -> list[WatchlistEntry]:
        seen: dict[tuple[str, str], int] = {}
        for index, entry in enumerate(value):
            key = (entry.ats, entry.token.lower())
            if key in seen:
                raise ValueError(
                    f"watchlist[{index}] is a duplicate of watchlist[{seen[key]}] "
                    f"({entry.ats}/{entry.token.lower()})"
                )
            seen[key] = index
        return value

    @model_validator(mode="after")
    def _blocklist_spares_the_watchlist(self) -> Self:
        names = {normalize_company(name): i for i, name in enumerate(self.blocklist.companies)}
        boards = {board.lower(): i for i, board in enumerate(self.blocklist.boards)}
        for index, entry in enumerate(self.watchlist):
            if not entry.active:
                continue
            board = f"{entry.ats}/{entry.token}".lower()
            name = normalize_company(entry.name)
            if board in boards:
                field = f"blocklist.boards[{boards[board]}]"
            elif name in names:
                field = f"blocklist.companies[{names[name]}]"
            else:
                continue
            raise ValueError(
                f"{field} blocks watchlist[{index}] ({entry.name}), which is active; remove one "
                "of them, or set active: false on the watchlist entry"
            )
        return self


def scoring_hash(profile: Profile) -> str:
    """Names the filters and scoring that open matches were scored with: when it changes,
    the worker re-scores them."""
    data = profile.model_dump(mode="json", include={"filters", "scoring"})
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


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


class Settings(BaseSettings):
    """Secrets and deployment overrides from the environment and ``.env``."""

    model_config = SettingsConfigDict(
        env_file=DEFAULT_ENV_FILE, env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore"
    )

    ntfy_topic: SecretStr | None = None
    ntfy_token: SecretStr | None = None
    discord_webhook_url: SecretStr | None = None
    llm_api_key: SecretStr | None = None
    dashboard_user: str | None = None
    dashboard_password: SecretStr | None = None
    database_url: str | None = None
    jobpulse_config: Path | None = None
    log_level: str = "INFO"

    def secret_values(self) -> list[str]:
        """Every secret string, for log redaction."""
        values = [
            secret.get_secret_value()
            for secret in (
                self.ntfy_topic,
                self.ntfy_token,
                self.discord_webhook_url,
                self.llm_api_key,
                self.dashboard_password,
            )
            if secret is not None
        ]
        if self.database_url and (password := urlsplit(self.database_url).password):
            values.append(password)
        return values


MOVED_TO_SETTINGS = (
    "moved to the database: edit it on Settings, or run `jobpulse profile import` once"
)
MOVED_TO_WATCHLIST = (
    "moved to the database: edit it on the Watchlist page, or run `jobpulse profile import` once"
)


def load_config(path: Path, *, missing_ok: bool = False) -> InfraConfig:
    """Read and validate ``config.yaml``, which holds infrastructure only; raise
    ``ConfigError`` naming every bad field. A preference key (``filters``, ``watchlist``…)
    is an error per key that says where it went. ``missing_ok``: no file means defaults."""
    data = _read_yaml(path, missing_ok=missing_ok)
    errors = [
        FieldError(key, MOVED_TO_WATCHLIST if key == "watchlist" else MOVED_TO_SETTINGS)
        for key in data
        if key in MOVED_KEYS
    ]
    rest = {key: value for key, value in data.items() if key not in MOVED_KEYS}
    try:
        config = InfraConfig.model_validate(rest)
    except ValidationError as exc:
        errors += describe_errors(exc)
    if errors:
        raise ConfigError(path, errors)
    return config


def read_config_lenient(path: Path, *, missing_ok: bool = True) -> InfraConfig:
    """Like ``load_config``, but preference keys from before M6 are dropped unread, so
    ``db upgrade`` and ``profile import`` work on an old config.yaml."""
    data = _read_yaml(path, missing_ok=missing_ok)
    try:
        return InfraConfig.model_validate(
            {key: value for key, value in data.items() if key not in MOVED_KEYS}
        )
    except ValidationError as exc:
        raise ConfigError(path, describe_errors(exc)) from None


def load_legacy_config(path: Path) -> LegacyConfig:
    """A config.yaml from before M6, preferences and watchlist included, validated whole."""
    data = _read_yaml(path, missing_ok=False)
    try:
        return LegacyConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(path, describe_errors(exc)) from None


def _read_yaml(path: Path, *, missing_ok: bool) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise ConfigError(
            path, [FieldError("", "file not found (copy config.example.yaml to config.yaml)")]
        ) from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(path, [FieldError("", _yaml_problem(exc))]) from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            path, [FieldError("", "the top level must be a mapping of section names to values")]
        )
    return data


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


def load_settings(env_file: Path | None) -> Settings:
    """Load secrets from the environment and ``env_file`` (a missing file is fine)."""
    try:
        return Settings(_env_file=env_file)
    except ValidationError as exc:
        errors = [
            FieldError(_format_location(e["loc"]).upper(), _describe(e)) for e in exc.errors()
        ]
        raise ConfigError(env_file or Path("<environment>"), errors) from None


def resolve_config_path(cli_value: Path | None, settings: Settings) -> Path:
    """``--config`` beats ``JOBPULSE_CONFIG``, which beats ``./config.yaml``."""
    return cli_value or settings.jobpulse_config or DEFAULT_CONFIG_PATH


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
    config: AppConfig, secrets: Mapping[str, str | None], *, env_file: Path, llm: bool = False
) -> None:
    """Check the secrets (``.env`` or Settings, resolved: ``credentials.SecretStore``) that
    the enabled notification channels need, and with ``llm`` the LLM layer's key when the
    layer is on (a live poll).

    Only commands that send call this; the worker starts without them and waits. Error
    messages never echo the secret values.
    """
    errors: list[FieldError] = []
    if llm and config.llm.enabled and not secrets.get("llm_api_key"):
        errors.append(
            FieldError(
                "LLM_API_KEY",
                "not set, but llm.enabled is true; add your Anthropic API key on Settings or "
                "in .env",
            )
        )
    for channel, name in (("ntfy", "ntfy_topic"), ("discord", "discord_webhook_url")):
        if channel not in config.notify.channels:
            continue
        value = secrets.get(name)
        if not value:
            errors.append(
                FieldError(name.upper(), f"not set, but notify.channels includes {channel!r}")
            )
        elif problem := secret_problem(name, value):
            errors.append(FieldError(name.upper(), problem))
    if errors:
        raise ConfigError(
            env_file,
            errors,
            heading="missing or invalid secrets (set them on Settings, or in {path})",
        )


def dashboard_credentials(settings: Settings, *, env_file: Path) -> tuple[str, str] | None:
    """(user, password) for the dashboard's basic auth when both are set, ``None`` when
    neither is. Setting only one is an error, so auth is never silently off."""
    user = settings.dashboard_user or ""
    password = settings.dashboard_password.get_secret_value() if settings.dashboard_password else ""
    if user and password:
        return user, password
    if not user and not password:
        return None
    missing = "DASHBOARD_PASSWORD" if user else "DASHBOARD_USER"
    raise ConfigError(
        env_file,
        [
            FieldError(
                missing,
                "not set; set both DASHBOARD_USER and DASHBOARD_PASSWORD to turn on the "
                "dashboard's basic auth, or neither to run it without (on 127.0.0.1 only)",
            )
        ],
        heading="missing or invalid secrets (set them in {path} or the environment)",
    )


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
    """Each validation error with its dotted field path, as config.yaml and Settings show
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
        ),
        blocklist=company_blocklist(config),
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
