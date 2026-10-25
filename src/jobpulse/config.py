"""Configuration: ``config.yaml`` validated with Pydantic, secrets from ``.env`` via pydantic-settings.

Every problem is reported as a ``ConfigError`` that names the file and a dotted field path
such as ``watchlist[3].ats``, so a typo is quick to find.
"""

import re
from collections.abc import Iterable
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
from jobpulse.core.dedupe import normalize_company
from jobpulse.core.locations import DEFAULT_SHORT_LABELS, merge_aliases
from jobpulse.core.scoring import Weights

DEFAULT_CONFIG_PATH = Path("config.yaml")
DEFAULT_ENV_FILE = Path(".env")
DEFAULT_DATABASE_URL = "sqlite:///data/jobpulse.db"
SIMPLIFY_NEW_GRAD_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/"
    "listings.json"
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


Regex = Annotated[str, AfterValidator(_check_regex)]
Slug = Annotated[str, Field(min_length=1, max_length=200, pattern=SLUG_PATTERN)]
BoardRef = Annotated[str, AfterValidator(_check_board_ref)]
CompanyName = Annotated[str, AfterValidator(_check_company_name)]


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
    aliases: dict[str, str] = {}
    unknown_location_passes: bool = True


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


class NotifyConfig(_Model):
    channels: list[NotifyChannel] = ["ntfy"]
    ntfy: NtfyConfig = NtfyConfig()
    discord: DiscordConfig = DiscordConfig()


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


class LlmConfig(_Model):
    enabled: bool = False
    provider: Literal["anthropic"] = "anthropic"
    model: str = Field(default="claude-opus-5-5", min_length=1)
    effort: LlmEffort = "low"
    resume_path: Path | None = None  # relative to config.yaml
    band: float = Field(default=0.10, gt=0, le=1)
    wanted_levels: list[Level] = []
    max_calls_per_day: int = Field(default=25, ge=1, le=1000)

    @model_validator(mode="after")
    def _resume_when_enabled(self) -> Self:
        if self.enabled and self.resume_path is None:
            raise ValueError(
                "resume_path is required when the LLM layer is enabled: a plain-text resume, "
                "relative to config.yaml"
            )
        return self


class DiscoveryConfig(_Model):
    enabled: bool = True
    require_match: bool = True
    max_board_jobs: int | None = Field(default=1000, ge=1)
    max_auto_companies: int | None = Field(default=50, ge=0)
    max_validations_per_cycle: int = Field(default=10, ge=0, le=100)
    max_page_fetches_per_cycle: int = Field(default=5, ge=0, le=50)


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


class AppConfig(_Model):
    contact_email: str
    timezone: str = "UTC"
    database_url: str = DEFAULT_DATABASE_URL
    http: HttpConfig = HttpConfig()
    schedules: SchedulesConfig = SchedulesConfig()
    detection: DetectionConfig = DetectionConfig()
    filters: FiltersConfig = FiltersConfig()
    scoring: ScoringConfig = ScoringConfig()
    notify: NotifyConfig = NotifyConfig()
    watchlist: list[WatchlistEntry] = []
    searches: list[SearchEntry] = []
    linkedin: LinkedInPacing = LinkedInPacing()
    workday: WorkdayConfig = WorkdayConfig()
    simplify: SimplifyConfig = SimplifyConfig()
    discovery: DiscoveryConfig = DiscoveryConfig()
    blocklist: BlocklistConfig = BlocklistConfig()
    health: HealthConfig = HealthConfig()
    retention: RetentionConfig = RetentionConfig()
    dashboard: DashboardConfig = DashboardConfig()
    llm: LlmConfig = LlmConfig()

    @field_validator("contact_email")
    @classmethod
    def _email(cls, value: str) -> str:
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

    @model_validator(mode="after")
    def _run_history_covers_the_busy_window(self) -> Self:
        days, window = self.retention.runs_days, self.health.busy_window_days
        if self.health.enabled and days is not None and days < window:
            raise ValueError(
                f"retention.runs_days ({days}) must be at least health.busy_window_days "
                f"({window:g}): the busy-source health rule reads that much run history"
            )
        return self

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


def load_config(path: Path) -> AppConfig:
    """Read and validate ``config.yaml``; raise ``ConfigError`` naming every bad field."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(
            path, [FieldError("", "file not found (copy config.example.yaml to config.yaml)")]
        ) from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(path, [FieldError("", _yaml_problem(exc))]) from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(
            path, [FieldError("", "the top level must be a mapping of section names to values")]
        )
    try:
        config = AppConfig.model_validate(data)
    except ValidationError as exc:
        errors = [FieldError(_format_location(e["loc"]), _describe(e)) for e in exc.errors()]
        raise ConfigError(path, errors) from None
    return _with_resume_beside(config, path)


def _with_resume_beside(config: AppConfig, path: Path) -> AppConfig:
    """``llm.resume_path`` relative to the config file; when the layer is on, it must be a
    readable, non-empty text file."""
    resume = config.llm.resume_path
    if resume is None:
        return config
    if not resume.is_absolute():
        resume = path.parent / resume
        config = config.model_copy(
            update={"llm": config.llm.model_copy(update={"resume_path": resume})}
        )
    if config.llm.enabled and (problem := _resume_problem(resume)):
        raise ConfigError(path, [FieldError("llm.resume_path", problem)])
    return config


def _resume_problem(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"file not found: {path}"
    except (OSError, UnicodeDecodeError) as exc:
        return f"can't be read as UTF-8 text ({type(exc).__name__}): {path}"
    return None if text.strip() else f"the file is empty: {path}"


def read_resume(config: AppConfig) -> str:
    """The resume the LLM layer compares postings with (``llm.resume_path``)."""
    path = config.llm.resume_path
    if path is None:
        raise ConfigError(DEFAULT_CONFIG_PATH, [FieldError("llm.resume_path", "not set")])
    problem = _resume_problem(path)
    if problem:
        raise ConfigError(path, [FieldError("llm.resume_path", problem)])
    return path.read_text(encoding="utf-8")


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


def require_secrets(
    config: AppConfig, settings: Settings, *, env_file: Path, llm: bool = False
) -> None:
    """Check the secrets that the enabled notification channels need, and with ``llm`` the
    LLM layer's key when the layer is on (commands that can call it: the worker and a live
    poll).

    Only commands that send call this; dry runs and ``config check`` without channels don't.
    Error messages never echo the secret values.
    """
    errors: list[FieldError] = []
    if (
        llm
        and config.llm.enabled
        and not (settings.llm_api_key and settings.llm_api_key.get_secret_value())
    ):
        errors.append(
            FieldError(
                "LLM_API_KEY",
                "not set, but llm.enabled is true; put your Anthropic API key in .env "
                "(see .env.example)",
            )
        )
    if "ntfy" in config.notify.channels:
        topic = settings.ntfy_topic.get_secret_value() if settings.ntfy_topic else ""
        if not topic:
            errors.append(
                FieldError(
                    "NTFY_TOPIC",
                    "not set, but notify.channels includes 'ntfy'; put a long random topic in "
                    ".env (see .env.example)",
                )
            )
        elif not NTFY_TOPIC_PATTERN.fullmatch(topic):
            errors.append(
                FieldError(
                    "NTFY_TOPIC",
                    "must be 24–64 characters of letters, digits, '_' or '-'; anyone who knows "
                    "or guesses the topic can read your alerts",
                )
            )
    if "discord" in config.notify.channels:
        webhook = (
            settings.discord_webhook_url.get_secret_value() if settings.discord_webhook_url else ""
        )
        if not webhook:
            errors.append(
                FieldError("DISCORD_WEBHOOK_URL", "not set, but notify.channels includes 'discord'")
            )
        elif not webhook.startswith("https://"):
            errors.append(FieldError("DISCORD_WEBHOOK_URL", "must be an https:// webhook URL"))
    if errors:
        raise ConfigError(
            env_file,
            errors,
            heading="missing or invalid secrets (set them in {path} or the environment)",
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
    if config.contact_email == EXAMPLE_CONTACT_EMAIL:
        warnings.append(
            "contact_email is still the example address; set yours so API operators can reach you"
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
        if search.site == "linkedin" and search.is_remote:
            warnings.append(
                f"searches[{index}] ({search.name}) sets is_remote, which LinkedIn ignores; "
                "put 'Remote' in its location or search_term instead"
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


def filter_rules(config: AppConfig) -> core_filters.FilterRules:
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
            unknown_passes=filters.locations.unknown_location_passes,
        ),
        blocklist=company_blocklist(config),
    )


def company_blocklist(config: AppConfig) -> core_filters.Blocklist:
    return core_filters.Blocklist.build(
        companies=config.blocklist.companies, boards=config.blocklist.boards
    )


def scoring_weights(config: AppConfig) -> Weights:
    weights = config.scoring.weights
    return Weights(
        freshness=weights.freshness,
        company_priority=weights.company_priority,
        title=weights.title,
        stack=weights.stack,
        level_mismatch=weights.level_mismatch,
        applicants=weights.applicants,
        llm=weights.llm,
    )


def location_aliases(config: AppConfig) -> dict[str, str]:
    return merge_aliases(config.filters.locations.aliases)


def short_labels(config: AppConfig) -> dict[str, str]:
    """Display names for push titles: built-ins plus short alias keys such as ``SF``."""
    reverse = {
        label: key for key, label in config.filters.locations.aliases.items() if len(key) <= 4
    }
    return {**DEFAULT_SHORT_LABELS, **reverse}
