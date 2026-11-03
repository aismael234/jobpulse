"""Settings validation: every error names the offending field.

Most of these validate the sections as YAML text through ``AppConfig`` (what the profile's
Advanced editor and the fixed defaults share). The end covers the profile on its own and
Settings → Sources & schedule (``infra_for``)."""

import hashlib
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from jobpulse.config import (
    PROFILE_VERSION,
    SIMPLIFY_NEW_GRAD_URL,
    AppConfig,
    ConfigError,
    InfraConfig,
    Profile,
    ProfileError,
    SourceSettings,
    company_blocklist,
    compose,
    config_warnings,
    describe_errors,
    filter_rules,
    infra_for,
    parse_profile,
    parse_profile_yaml,
    profile_data,
    profile_yaml,
    require_secrets,
    scoring_hash,
    split,
)
from jobpulse.core.filters import Blocklist
from jobpulse.data import agency_names

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PROFILE = REPO_ROOT / "profile.example.yaml"
MINIMAL = "contact_email: dev@example.com\n"
GOOD_TOPIC = "jobpulse-Zq3vX9kR2mT7wL4pB8nC1dF6"


def write_config(tmp_path: Path, body: str = "") -> Path:
    path = tmp_path / "settings.yaml"
    path.write_text(MINIMAL + textwrap.dedent(body), encoding="utf-8")
    return path


def load(path: Path) -> AppConfig:
    """The YAML at ``path`` as one ``AppConfig``; ``ConfigError`` names each bad field."""
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(describe_errors(exc)) from None


def config_error(path: Path) -> str:
    with pytest.raises(ConfigError) as excinfo:
        load(path)
    return str(excinfo.value)


def secrets_error(config_path: Path, secrets: dict[str, str]) -> str:
    config = load(config_path)
    with pytest.raises(ConfigError) as excinfo:
        require_secrets(config, secrets)
    return str(excinfo.value)


def example() -> AppConfig:
    """profile.example.yaml with the fixed defaults, as a fresh install composes them."""
    profile = parse_profile_yaml(EXAMPLE_PROFILE.read_text(encoding="utf-8"))
    return compose(infra_for(profile), profile)


# --- config.yaml ------------------------------------------------------------------


def test_the_example_profile_is_valid() -> None:
    config = example()
    assert config.notify.channels == ["ntfy"]


def test_minimal_config_gets_the_defaults(tmp_path: Path) -> None:
    config = load(write_config(tmp_path))
    assert config.detection.freshness_hours == 48
    assert config.detection.closure_missing_polls == 2
    assert config.detection.fingerprint_window_days == 30
    assert config.schedules.watchlist.every_minutes == 20
    assert config.schedules.simplify.every_minutes == 20
    assert config.schedules.job_boards.every_minutes == 30
    assert config.schedules.watchlist.jitter_seconds == 120
    assert config.http.timeout_seconds == 20
    assert config.http.max_attempts == 4
    assert config.linkedin.delay_min_seconds == 15
    assert config.linkedin.delay_max_seconds == 30


def test_bad_regex_names_the_field(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        r"""
        filters:
          title:
            exclude: ['\bok\b', '(unclosed']
        """,
    )
    message = config_error(path)
    assert "filters.title.exclude[1]" in message
    assert "invalid regular expression" in message


def test_bad_regex_in_weighted_include(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        filters:
          title:
            include:
              - {pattern: 'backend[', weight: 1.0}
        """,
    )
    assert "filters.title.include[0].pattern" in config_error(path)


def test_bad_regex_as_stack_keyword(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        filters:
          description:
            stack_keywords: {'go(lang': 1.0}
        """,
    )
    message = config_error(path)
    assert "filters.description.stack_keywords" in message
    assert "invalid regular expression" in message


def test_negative_interval(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        schedules:
          watchlist: {every_minutes: -5}
        """,
    )
    message = config_error(path)
    assert "schedules.watchlist.every_minutes" in message
    assert "greater than 0" in message


def test_jitter_must_be_shorter_than_interval(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        schedules:
          watchlist: {every_minutes: 1, jitter_seconds: 90}
        """,
    )
    message = config_error(path)
    assert "schedules.watchlist" in message
    assert "jitter" in message


def test_unknown_key_is_reported(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        detection:
          freshnes_hours: 12
        """,
    )
    message = config_error(path)
    assert "detection.freshnes_hours" in message
    assert "unknown key" in message


def test_unknown_notify_channel(tmp_path: Path) -> None:
    path = write_config(tmp_path, "notify: {channels: [sms]}\n")
    assert "notify.channels[0]" in config_error(path)


def test_invalid_timezone(tmp_path: Path) -> None:
    path = write_config(tmp_path, "timezone: Mars/Olympus_Mons\n")
    message = config_error(path)
    assert "timezone" in message
    assert "Mars/Olympus_Mons" in message


def test_invalid_contact_email(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("contact_email: not-an-email\n", encoding="utf-8")
    assert "contact_email" in config_error(path)


def test_contact_email_must_be_ascii(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("contact_email: josé@example.com\n", encoding="utf-8")
    message = config_error(path)
    assert "contact_email" in message
    assert "ASCII" in message


def test_placeholders_produce_warnings_not_errors() -> None:
    warnings = config_warnings(example())
    assert any("filters.level.include" in w and "<SET ME>" in w for w in warnings)
    assert any("contact_email" in w for w in warnings)


# --- Milestone 2 sections -------------------------------------------------------------


def test_m2_sections_get_their_defaults(tmp_path: Path) -> None:
    config = load(write_config(tmp_path))
    assert config.simplify.enabled
    assert config.simplify.name == "new-grad"
    assert config.simplify.url == SIMPLIFY_NEW_GRAD_URL
    assert config.discovery.enabled
    assert config.discovery.max_validations_per_cycle == 20
    assert config.discovery.max_page_fetches_per_cycle == 5
    assert config.health.consecutive_failures == 3
    assert config.health.busy_min == 5
    assert config.health.busy_window_days == 7
    assert config.health.zero_hours == 24
    assert config.linkedin.cooldown_minutes == 30
    assert config.schedules.job_boards.every_minutes == 30
    assert config.schedules.job_boards.jitter_seconds == 900


def test_example_config_has_m2_sections() -> None:
    config = example()
    assert config.simplify.url == SIMPLIFY_NEW_GRAD_URL
    assert config.searches == []


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ("simplify: {url: 'http://example.com/listings.json'}\n", "simplify.url"),
        ("simplify: {name: 'has space'}\n", "simplify.name"),
        ("discovery: {max_validations_per_cycle: -1}\n", "discovery.max_validations_per_cycle"),
        ("health: {consecutive_failures: 0}\n", "health.consecutive_failures"),
        ("health: {zero_hours: 0}\n", "health.zero_hours"),
        ("linkedin: {cooldown_minutes: -5}\n", "linkedin.cooldown_minutes"),
        (
            "searches:\n  - {name: s, site: glassdoor, search_term: backend}\n",
            "searches[0].site",
        ),
    ],
)
def test_m2_sections_name_bad_fields(tmp_path: Path, body: str, field: str) -> None:
    path = write_config(tmp_path, body)
    message = config_error(path)
    assert field in message


def test_linkedin_is_remote_produces_a_warning(tmp_path: Path) -> None:
    config = load(
        write_config(
            tmp_path,
            """
            searches:
              - {name: remote-li, site: linkedin, search_term: backend engineer, is_remote: true}
              - {name: remote-in, site: indeed, search_term: backend engineer, is_remote: true}
            """,
        )
    )
    [warning] = [w for w in config_warnings(config) if "is_remote" in w]
    assert "searches[0]" in warning
    assert "LinkedIn" in warning
    assert "search_term" in warning


def test_a_linkedin_search_for_remote_produces_a_warning(tmp_path: Path) -> None:
    """LinkedIn reads a location of "Remote" as anywhere in the world."""
    config = load(
        write_config(
            tmp_path,
            """
            searches:
              - {name: ml-us, site: linkedin, search_term: ml engineer, location: United States}
              - {name: ml-remote, site: linkedin, search_term: ml engineer, location: remote}
              - {name: ml-in, site: indeed, search_term: ml engineer, location: Remote}
            """,
        )
    )
    [warning] = [w for w in config_warnings(config) if "worldwide" in w]
    assert warning.startswith("searches[1] (ml-remote)")
    assert "United States" in warning
    assert "filters.locations.countries" in warning


def test_unknown_country_names_field_and_value(tmp_path: Path) -> None:
    path = write_config(tmp_path, "filters:\n  locations:\n    countries: [US, Narnia]\n")
    message = config_error(path)
    assert "filters.locations.countries[1]" in message
    assert "'Narnia'" in message
    assert "ISO 3166" in message


# --- secrets saved on Settings -------------------------------------------------------------


def test_a_missing_ntfy_topic_is_named(tmp_path: Path) -> None:
    message = secrets_error(write_config(tmp_path, "notify: {channels: [ntfy]}\n"), {})
    assert "ntfy topic" in message
    assert "Settings" in message


def test_short_ntfy_topic_is_rejected_without_echoing_it(tmp_path: Path) -> None:
    message = secrets_error(write_config(tmp_path), {"ntfy_topic": "abc123"})
    assert "ntfy topic" in message
    assert "abc123" not in message


def test_ntfy_topic_with_bad_characters_is_rejected(tmp_path: Path) -> None:
    topic = "this topic has spaces but is long enough"
    message = secrets_error(write_config(tmp_path), {"ntfy_topic": topic})
    assert topic not in message


def test_valid_ntfy_topic_passes(tmp_path: Path) -> None:
    require_secrets(load(write_config(tmp_path)), {"ntfy_topic": GOOD_TOPIC})


def test_discord_channel_requires_webhook(tmp_path: Path) -> None:
    message = secrets_error(write_config(tmp_path, "notify: {channels: [discord]}\n"), {})
    assert "Discord webhook URL" in message


def test_discord_webhook_must_be_https(tmp_path: Path) -> None:
    webhook = "http://example.com/api/webhooks/1/abc"
    message = secrets_error(
        write_config(tmp_path, "notify: {channels: [discord]}\n"),
        {"discord_webhook_url": webhook},
    )
    assert "Discord webhook URL" in message
    assert webhook not in message


def test_no_channels_needs_no_secrets(tmp_path: Path) -> None:
    require_secrets(load(write_config(tmp_path, "notify: {channels: []}\n")), {})


# --- narrower discovery and the company blocklist -----------------------------------------


def test_discovery_narrowing_and_blocklist_defaults(tmp_path: Path) -> None:
    config = load(write_config(tmp_path))
    assert config.discovery.require_match is True
    assert config.discovery.max_board_jobs == 1000
    assert config.discovery.max_auto_companies == 1500
    assert config.blocklist.companies == []
    assert config.blocklist.boards == []
    # hide_agencies is on by default: the staffing agencies are the whole base
    assert config.hide_agencies is True
    assert company_blocklist(config) == Blocklist.build(companies=agency_names())
    assert company_blocklist(config.model_copy(update={"hide_agencies": False})) == Blocklist()


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ("blocklist: {boards: [spacex]}\n", "blocklist.boards[0]"),
        ("blocklist: {boards: [greenhouse/acme, 'workable/acme']}\n", "blocklist.boards[1]"),
        ("blocklist: {companies: ['Inc.']}\n", "blocklist.companies[0]"),
        ("blocklist: {companies: [Acme, '']}\n", "blocklist.companies[1]"),
        ("discovery: {max_board_jobs: 0}\n", "discovery.max_board_jobs"),
        ("discovery: {max_auto_companies: -1}\n", "discovery.max_auto_companies"),
    ],
)
def test_blocklist_and_narrowing_name_bad_fields(tmp_path: Path, body: str, field: str) -> None:
    message = config_error(write_config(tmp_path, body))
    assert field in message


def test_limits_can_be_turned_off(tmp_path: Path) -> None:
    config = load(
        write_config(tmp_path, "discovery: {max_board_jobs: null, max_auto_companies: null}\n")
    )
    assert config.discovery.max_board_jobs is None
    assert config.discovery.max_auto_companies is None


def test_the_blocklist_compiles_into_the_filter_rules(tmp_path: Path) -> None:
    config = load(
        write_config(
            tmp_path,
            "blocklist: {companies: ['Jobot', 'Revature LLC'], boards: [greenhouse/SpaceX]}\n",
        )
    )
    blocklist = company_blocklist(config)
    assert blocklist == Blocklist.build(
        companies=["Jobot", "Revature", *agency_names()], boards=["greenhouse/spacex"]
    )
    assert filter_rules(config).blocklist == blocklist


# --- run history retention ------------------------------------------------------------------


def test_run_history_is_kept_for_30_days_by_default(tmp_path: Path) -> None:
    assert load(write_config(tmp_path)).retention.runs_days == 30


def test_run_history_can_be_kept_forever(tmp_path: Path) -> None:
    config = load(write_config(tmp_path, "retention: {runs_days: null}\n"))
    assert config.retention.runs_days is None


@pytest.mark.parametrize("days", ["0", "-3", "1.5"])
def test_run_history_needs_whole_positive_days(tmp_path: Path, days: str) -> None:
    path = write_config(tmp_path, f"retention: {{runs_days: {days}}}\n")
    assert "retention.runs_days" in config_error(path)


def test_run_history_must_cover_the_busy_window(tmp_path: Path) -> None:
    message = config_error(write_config(tmp_path, "retention: {runs_days: 3}\n"))
    assert "retention.runs_days" in message
    assert "health.busy_window_days" in message
    # without health checks nothing reads that far back
    config = load(write_config(tmp_path, "retention: {runs_days: 3}\nhealth: {enabled: false}\n"))
    assert config.retention.runs_days == 3


# --- Milestone 4 sections -------------------------------------------------------------


@pytest.mark.parametrize("wait", ["12", "20"])
def test_job_pages_wait_less_than_the_freshness_window(tmp_path: Path, wait: str) -> None:
    body = f"detection: {{freshness_hours: 12}}\nlinkedin: {{job_page_wait_hours: {wait}}}\n"
    message = config_error(write_config(tmp_path, body))
    assert f"linkedin.job_page_wait_hours ({wait}) must be less than" in message
    assert "detection.freshness_hours (12)" in message


def test_m4_sections_get_their_defaults(tmp_path: Path) -> None:
    config = load(write_config(tmp_path))
    assert config.linkedin.job_pages
    assert config.linkedin.max_job_pages_per_hour == 40
    assert config.linkedin.job_page_wait_hours == 12
    assert config.workday.max_pages == 25
    assert config.scoring.applicant_cap == 200
    assert config.scoring.weights.llm == 0.15
    assert config.llm.enabled is False  # off by default
    assert config.llm.provider == "anthropic"
    assert config.llm.model == "claude-opus-5-5"
    assert config.llm.effort == "low"
    assert config.llm.band == 0.10
    assert config.llm.wanted_levels == []
    assert config.llm.max_calls_per_day == 25
    assert config.llm.role == "backend software engineer"


def test_the_example_profile_keeps_the_llm_off() -> None:
    assert example().llm.enabled is False


def test_a_workday_board_can_be_blocked(tmp_path: Path) -> None:
    config = load(write_config(tmp_path, "blocklist: {boards: [workday/omega.wd5/External]}\n"))
    assert company_blocklist(config).blocks_board("workday", "omega.wd5/External")


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ("llm: {effort: extreme}\n", "llm.effort"),
        ("llm: {provider: openai}\n", "llm.provider"),
        ("llm: {wanted_levels: [director]}\n", "llm.wanted_levels[0]"),
        ("llm: {band: 0}\n", "llm.band"),
        ("llm: {max_calls_per_day: 0}\n", "llm.max_calls_per_day"),
        ("llm: {model: ''}\n", "llm.model"),
        ("llm: {role: ''}\n", "llm.role"),
        ("llm: {resume_path: resume.txt}\n", "llm.resume_path"),
        ("scoring: {applicant_cap: 0}\n", "scoring.applicant_cap"),
        ("scoring: {weights: {llm: 2}}\n", "scoring.weights.llm"),
        ("workday: {max_pages: 0}\n", "workday.max_pages"),
        ("workday: {max_pages: 101}\n", "workday.max_pages"),
        ("linkedin: {max_job_pages_per_hour: 0}\n", "linkedin.max_job_pages_per_hour"),
    ],
)
def test_m4_sections_name_bad_fields(tmp_path: Path, body: str, field: str) -> None:
    path = write_config(tmp_path, body)
    message = config_error(path)
    assert field in message


def llm_config(tmp_path: Path) -> Path:
    return write_config(tmp_path, "notify: {channels: []}\nllm: {enabled: true}\n")


def test_the_llm_key_is_required_by_commands_that_call_the_llm(tmp_path: Path) -> None:
    config = load(llm_config(tmp_path))
    with pytest.raises(ConfigError) as excinfo:
        require_secrets(config, {}, llm=True)
    assert "Anthropic API key" in str(excinfo.value)
    require_secrets(config, {})  # notify test: no LLM


def test_a_set_llm_key_passes_and_is_never_echoed(tmp_path: Path) -> None:
    config = load(llm_config(tmp_path))
    require_secrets(config, {"llm_api_key": "sk-ant-secret-0123456789"}, llm=True)


def test_the_llm_key_is_not_needed_while_the_llm_is_off(tmp_path: Path) -> None:
    require_secrets(load(write_config(tmp_path, "notify: {channels: []}\n")), {}, llm=True)


# --- Sources & schedule: the settings that change how the worker runs ---------------------


def test_new_installs_start_from_the_tuned_values() -> None:
    sources = SourceSettings()
    assert (sources.check_every_minutes, sources.search_every_minutes) == (20, 30)
    assert (sources.linkedin_pause_min_seconds, sources.linkedin_pause_max_seconds) == (15, 30)
    assert sources.discovery_max_companies == 1500
    infra = infra_for(None)
    assert infra == infra_for(Profile())
    assert infra.discovery.max_validations_per_cycle == 20


def test_sources_and_schedule_shape_how_the_worker_runs(tmp_path: Path) -> None:
    sources = SourceSettings(
        check_every_minutes=10,
        search_every_minutes=60,
        freshness_hours=24,
        linkedin_pause_min_seconds=5,
        linkedin_pause_max_seconds=9,
        linkedin_job_pages=False,
        linkedin_job_pages_per_hour=10,
        simplify_feed=False,
        discovery=False,
        discovery_max_companies=7,
        seed_updates=True,
        health_alerts=False,
        resume_matching=False,
    )
    infra = infra_for(Profile(sources=sources), data_dir=tmp_path)
    assert infra.schedules.watchlist.every_minutes == 10
    assert infra.schedules.simplify.every_minutes == 10
    assert infra.schedules.job_boards.every_minutes == 60
    assert infra.schedules.watchlist.jitter_seconds == 120
    assert infra.detection.freshness_hours == 24
    assert (infra.linkedin.delay_min_seconds, infra.linkedin.delay_max_seconds) == (5, 9)
    assert (infra.linkedin.job_pages, infra.linkedin.max_job_pages_per_hour) == (False, 10)
    assert not infra.simplify.enabled
    assert (infra.discovery.enabled, infra.discovery.max_auto_companies) == (False, 7)
    assert infra.seed.remote
    assert not infra.health.enabled
    assert not infra.embeddings.enabled
    assert infra.embeddings.cache_dir == tmp_path / "models"


def test_a_short_interval_keeps_its_jitter_under_half_of_it() -> None:
    infra = infra_for(Profile(sources=SourceSettings(search_every_minutes=15)))
    assert infra.schedules.job_boards.jitter_seconds == 450  # 900 s would exceed the interval


def test_the_base_keeps_what_settings_dont_set() -> None:
    base = InfraConfig.model_validate({"discovery": {"max_page_fetches_per_cycle": 0}})
    infra = infra_for(Profile(), base=base)
    assert infra.discovery.max_page_fetches_per_cycle == 0
    assert infra.discovery.max_auto_companies == 1500


def test_jobpulse_models_dir_beats_the_data_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBPULSE_MODELS_DIR", str(tmp_path / "baked"))
    assert infra_for(None, data_dir=tmp_path).embeddings.cache_dir == tmp_path / "baked"


@pytest.mark.parametrize(
    ("values", "field"),
    [
        ({"check_every_minutes": 1}, "check_every_minutes"),
        ({"search_every_minutes": 5}, "search_every_minutes"),
        ({"freshness_hours": 12}, "freshness_hours"),
        ({"linkedin_pause_min_seconds": 40, "linkedin_pause_max_seconds": 30}, ""),
        ({"discovery_max_companies": -1}, "discovery_max_companies"),
        ({"retired": True}, "retired"),
    ],
)
def test_bad_sources_settings_name_the_field(values: dict[str, Any], field: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        SourceSettings.model_validate(values)
    fields = [error.field for error in describe_errors(excinfo.value)]
    assert field in fields


# --- the profile ----------------------------------------------------------------------------


def test_compose_and_split_are_inverses(tmp_path: Path) -> None:
    legacy = load(
        write_config(
            tmp_path,
            """
            timezone: America/Los_Angeles
            detection: {freshness_hours: 24}
            filters: {title: {include: [{pattern: backend, weight: 0.9}]}}
            searches: [{name: s, site: indeed, search_term: backend}]
            """,
        )
    )
    infra, profile = split(legacy)
    config = compose(infra, profile)
    assert compose(*split(config)) == config
    assert config.detection.freshness_hours == 24
    assert config.filters == legacy.filters
    assert filter_rules(config) == filter_rules(legacy)  # the same compiled rules


def test_the_profile_round_trips_through_its_stored_json() -> None:
    profile = split(example())[1]
    assert parse_profile(profile_data(profile)) == profile
    assert parse_profile_yaml(profile_yaml(profile)) == profile
    assert profile_data(profile)["version"] == PROFILE_VERSION


def test_profile_errors_name_each_field() -> None:
    with pytest.raises(ProfileError) as excinfo:
        parse_profile_yaml(
            "timezone: Mars/Base\nfilters: {title: {include: ['(']}}\ndatabase_url: x\n"
        )
    fields = {error.field for error in excinfo.value.errors}
    assert fields == {"timezone", "filters.title.include[0].pattern", "database_url"}


@pytest.mark.parametrize(
    ("text", "problem"),
    [("- a\n", "mapping"), ("filters: [\n", "line 2"), ("version: 99\n", "newer jobpulse")],
)
def test_a_profile_that_cant_be_read_says_why(text: str, problem: str) -> None:
    with pytest.raises(ProfileError, match=problem):
        parse_profile_yaml(text)


def test_an_empty_profile_is_the_defaults() -> None:
    assert parse_profile_yaml("") == Profile()
    assert Profile().contact_email is None


def test_the_scoring_hash_follows_filters_and_scoring_only() -> None:
    base = Profile()
    weights = Profile.model_validate({"scoring": {"weights": {"stack": 0.3}}})
    stack = Profile.model_validate({"filters": {"description": {"stack_keywords": {"go": 1}}}})
    timezone = Profile.model_validate({"timezone": "America/New_York"})
    assert len({scoring_hash(base), scoring_hash(weights), scoring_hash(stack)}) == 3
    assert scoring_hash(base) == scoring_hash(timezone)


def test_settings_left_at_their_defaults_never_move_the_scoring_hash() -> None:
    """A new jobpulse that adds a setting with a default doesn't re-score every open match."""
    assert scoring_hash(Profile()) == hashlib.sha256(b"{}").hexdigest()
    explicit = Profile.model_validate(
        {"filters": {"pay": {"min_yearly_usd": None}, "locations": {"places": []}}}
    )
    assert scoring_hash(explicit) == scoring_hash(Profile())
