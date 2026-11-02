"""Config validation: every error names the offending field and the file it came from.

Most of these validate the sections through ``load_legacy_config``, which reads the whole
pre-M6 shape (preferences and watchlist included), as `jobpulse profile import` does; the
profile and config.yaml share those models. The M6 section at the end covers the split:
config.yaml with infrastructure only, and the profile parsed on its own."""

import textwrap
from pathlib import Path

import pytest

from jobpulse.config import (
    DEFAULT_DATABASE_URL,
    PROFILE_VERSION,
    SIMPLIFY_NEW_GRAD_URL,
    AppConfig,
    ConfigError,
    InfraConfig,
    Profile,
    ProfileError,
    company_blocklist,
    compose,
    config_warnings,
    dashboard_credentials,
    filter_rules,
    load_config,
    load_legacy_config,
    load_settings,
    parse_profile,
    parse_profile_yaml,
    profile_data,
    profile_yaml,
    read_config_lenient,
    require_secrets,
    scoring_hash,
    split,
)
from jobpulse.core.filters import Blocklist
from jobpulse.credentials import env_secrets

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config.example.yaml"
EXAMPLE_PROFILE = REPO_ROOT / "profile.example.yaml"
MINIMAL = "contact_email: dev@example.com\n"
GOOD_TOPIC = "jobpulse-Zq3vX9kR2mT7wL4pB8nC1dF6"


def write_config(tmp_path: Path, body: str = "") -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL + textwrap.dedent(body), encoding="utf-8")
    return path


def write_env(tmp_path: Path, body: str = "") -> Path:
    path = tmp_path / ".env"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def config_error(path: Path) -> str:
    with pytest.raises(ConfigError) as excinfo:
        load_legacy_config(path)
    return str(excinfo.value)


def secrets_error(config_path: Path, env_path: Path) -> str:
    config = load_legacy_config(config_path)
    with pytest.raises(ConfigError) as excinfo:
        require_secrets(config, env_secrets(load_settings(env_path)), env_file=env_path)
    return str(excinfo.value)


def example() -> AppConfig:
    """config.example.yaml with profile.example.yaml, as a fresh install composes them."""
    return compose(
        load_config(EXAMPLE_CONFIG),
        parse_profile_yaml(EXAMPLE_PROFILE.read_text(encoding="utf-8")),
    )


# --- config.yaml ------------------------------------------------------------------


def test_example_files_are_valid() -> None:
    config = example()
    assert config.notify.channels == ["ntfy"]
    assert config.database_url == DEFAULT_DATABASE_URL


def test_minimal_config_gets_spec_defaults(tmp_path: Path) -> None:
    config = load_legacy_config(write_config(tmp_path))
    assert config.database_url == DEFAULT_DATABASE_URL
    assert config.detection.freshness_hours == 48
    assert config.detection.closure_missing_polls == 2
    assert config.detection.fingerprint_window_days == 30
    assert config.schedules.watchlist.every_minutes == 30
    assert config.schedules.watchlist.jitter_seconds == 120
    assert config.http.timeout_seconds == 20
    assert config.http.max_attempts == 4
    assert config.linkedin.delay_min_seconds == 4
    assert config.linkedin.delay_max_seconds == 8
    assert config.dashboard.host == "127.0.0.1"


def test_bad_regex_names_field_and_file(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        r"""
        filters:
          title:
            exclude: ['\bok\b', '(unclosed']
        """,
    )
    message = config_error(path)
    assert str(path) in message
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


def test_unknown_ats_type_names_field_and_value(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        watchlist:
          - {name: Acme, ats: smartrecruiters, token: acme}
        """,
    )
    message = config_error(path)
    assert str(path) in message
    assert "watchlist[0].ats" in message
    assert "smartrecruiters" in message


def test_negative_interval(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        schedules:
          watchlist: {every_minutes: -5}
        """,
    )
    message = config_error(path)
    assert str(path) in message
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


def test_duplicate_watchlist_entry(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        watchlist:
          - {name: Acme, ats: lever, token: acme}
          - {name: Acme again, ats: lever, token: ACME}
        """,
    )
    message = config_error(path)
    assert "watchlist[1]" in message
    assert "duplicate" in message


def test_unknown_notify_channel(tmp_path: Path) -> None:
    path = write_config(tmp_path, "notify: {channels: [sms]}\n")
    assert "notify.channels[0]" in config_error(path)


def test_invalid_timezone(tmp_path: Path) -> None:
    path = write_config(tmp_path, "timezone: Mars/Olympus_Mons\n")
    message = config_error(path)
    assert "timezone" in message
    assert "Mars/Olympus_Mons" in message


def test_invalid_contact_email(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("contact_email: not-an-email\n", encoding="utf-8")
    assert "contact_email" in config_error(path)


def test_contact_email_must_be_ascii(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("contact_email: josé@example.com\n", encoding="utf-8")
    message = config_error(path)
    assert "contact_email" in message
    assert "ASCII" in message


def test_yaml_syntax_error_reports_file_and_line(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        watchlist:
          - {name: Acme, ats: lever
        """,
    )
    message = config_error(path)
    assert str(path) in message
    assert "line " in message


def test_missing_file(tmp_path: Path) -> None:
    message = config_error(tmp_path / "nope.yaml")
    assert "nope.yaml" in message
    assert "not found" in message


def test_top_level_must_be_a_mapping(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    assert "mapping" in config_error(path)


def test_placeholders_produce_warnings_not_errors() -> None:
    warnings = config_warnings(example())
    assert any("filters.level.include" in w and "<SET ME>" in w for w in warnings)
    assert any("contact_email" in w for w in warnings)


# --- Milestone 2 sections -------------------------------------------------------------


def test_m2_sections_get_their_defaults(tmp_path: Path) -> None:
    config = load_legacy_config(write_config(tmp_path))
    assert config.simplify.enabled
    assert config.simplify.name == "new-grad"
    assert config.simplify.url == SIMPLIFY_NEW_GRAD_URL
    assert config.discovery.enabled
    assert config.discovery.max_validations_per_cycle == 10
    assert config.discovery.max_page_fetches_per_cycle == 5
    assert config.health.consecutive_failures == 3
    assert config.health.busy_min == 5
    assert config.health.busy_window_days == 7
    assert config.health.zero_hours == 24
    assert config.linkedin.cooldown_minutes == 30
    assert config.schedules.job_boards.every_minutes == 75
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
    assert str(path) in message


def test_linkedin_is_remote_produces_a_warning(tmp_path: Path) -> None:
    config = load_legacy_config(
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
    config = load_legacy_config(
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


# --- secrets from .env --------------------------------------------------------------


def test_missing_ntfy_topic_names_variable_and_env_file(tmp_path: Path) -> None:
    message = secrets_error(
        write_config(tmp_path, "notify: {channels: [ntfy]}\n"), write_env(tmp_path)
    )
    assert "NTFY_TOPIC" in message
    assert str(tmp_path / ".env") in message
    assert "notify.channels" in message


def test_short_ntfy_topic_is_rejected_without_echoing_it(tmp_path: Path) -> None:
    message = secrets_error(write_config(tmp_path), write_env(tmp_path, "NTFY_TOPIC=abc123\n"))
    assert "NTFY_TOPIC" in message
    assert "abc123" not in message


def test_ntfy_topic_with_bad_characters_is_rejected(tmp_path: Path) -> None:
    topic = "this topic has spaces but is long enough"
    message = secrets_error(write_config(tmp_path), write_env(tmp_path, f"NTFY_TOPIC='{topic}'\n"))
    assert "NTFY_TOPIC" in message
    assert topic not in message


def test_valid_ntfy_topic_passes(tmp_path: Path) -> None:
    config = load_legacy_config(write_config(tmp_path))
    env_file = write_env(tmp_path, f"NTFY_TOPIC={GOOD_TOPIC}\n")
    require_secrets(config, env_secrets(load_settings(env_file)), env_file=env_file)


def test_environment_variable_satisfies_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NTFY_TOPIC", GOOD_TOPIC)
    config = load_legacy_config(write_config(tmp_path))
    env_file = write_env(tmp_path)
    require_secrets(config, env_secrets(load_settings(env_file)), env_file=env_file)


def test_discord_channel_requires_webhook(tmp_path: Path) -> None:
    message = secrets_error(
        write_config(tmp_path, "notify: {channels: [discord]}\n"), write_env(tmp_path)
    )
    assert "DISCORD_WEBHOOK_URL" in message
    assert "notify.channels" in message


def test_discord_webhook_must_be_https(tmp_path: Path) -> None:
    webhook = "http://example.com/api/webhooks/1/abc"
    message = secrets_error(
        write_config(tmp_path, "notify: {channels: [discord]}\n"),
        write_env(tmp_path, f"DISCORD_WEBHOOK_URL={webhook}\n"),
    )
    assert "DISCORD_WEBHOOK_URL" in message
    assert webhook not in message


def test_no_channels_needs_no_secrets(tmp_path: Path) -> None:
    config = load_legacy_config(write_config(tmp_path, "notify: {channels: []}\n"))
    env_file = write_env(tmp_path)
    require_secrets(config, env_secrets(load_settings(env_file)), env_file=env_file)


def test_settings_mask_secrets_in_repr(tmp_path: Path) -> None:
    settings = load_settings(write_env(tmp_path, f"NTFY_TOPIC={GOOD_TOPIC}\n"))
    assert GOOD_TOPIC not in repr(settings)
    assert settings.ntfy_topic is not None
    assert settings.ntfy_topic.get_secret_value() == GOOD_TOPIC


def test_empty_env_value_counts_as_unset(tmp_path: Path) -> None:
    settings = load_settings(write_env(tmp_path, "NTFY_TOPIC=\nDISCORD_WEBHOOK_URL=\n"))
    assert settings.ntfy_topic is None
    assert settings.discord_webhook_url is None


# --- narrower discovery and the company blocklist -----------------------------------------


def test_discovery_narrowing_and_blocklist_defaults(tmp_path: Path) -> None:
    config = load_legacy_config(write_config(tmp_path))
    assert config.discovery.require_match is True
    assert config.discovery.max_board_jobs == 1000
    assert config.discovery.max_auto_companies == 50
    assert config.blocklist.companies == []
    assert config.blocklist.boards == []
    assert company_blocklist(config) == Blocklist()


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
    config = load_legacy_config(
        write_config(tmp_path, "discovery: {max_board_jobs: null, max_auto_companies: null}\n")
    )
    assert config.discovery.max_board_jobs is None
    assert config.discovery.max_auto_companies is None


@pytest.mark.parametrize(
    "blocklist",
    ["{companies: ['ACME, Inc']}", "{boards: [greenhouse/Acme]}"],
    ids=["by-name", "by-board"],
)
def test_blocking_an_active_watchlist_company_is_an_error(tmp_path: Path, blocklist: str) -> None:
    path = write_config(
        tmp_path,
        f"""
        watchlist:
          - {{name: Beta, ats: lever, token: beta}}
          - {{name: Acme, ats: greenhouse, token: acme}}
        blocklist: {blocklist}
        """,
    )
    message = config_error(path)
    assert "watchlist[1]" in message
    assert "blocklist." in message
    assert str(path) in message


def test_an_inactive_watchlist_company_may_be_blocked(tmp_path: Path) -> None:
    config = load_legacy_config(
        write_config(
            tmp_path,
            """
            watchlist:
              - {name: Acme, ats: greenhouse, token: acme, active: false}
            blocklist: {companies: [Acme], boards: [greenhouse/acme]}
            """,
        )
    )
    assert config.blocklist.companies == ["Acme"]


def test_the_blocklist_compiles_into_the_filter_rules(tmp_path: Path) -> None:
    config = load_legacy_config(
        write_config(
            tmp_path,
            "blocklist: {companies: ['Jobot', 'Revature LLC'], boards: [greenhouse/SpaceX]}\n",
        )
    )
    blocklist = company_blocklist(config)
    assert blocklist == Blocklist.build(
        companies=["Jobot", "Revature"], boards=["greenhouse/spacex"]
    )
    assert filter_rules(config).blocklist == blocklist


# --- dashboard basic auth (Milestone 3) ---------------------------------------------------


def test_dashboard_auth_is_off_without_credentials(tmp_path: Path) -> None:
    env_file = write_env(tmp_path)
    assert dashboard_credentials(load_settings(env_file), env_file=env_file) is None


def test_dashboard_auth_needs_both_values(tmp_path: Path) -> None:
    env_file = write_env(tmp_path, "DASHBOARD_USER=me\nDASHBOARD_PASSWORD=correct-horse\n")
    assert dashboard_credentials(load_settings(env_file), env_file=env_file) == (
        "me",
        "correct-horse",
    )


@pytest.mark.parametrize(
    ("body", "missing"),
    [
        ("DASHBOARD_USER=me\n", "DASHBOARD_PASSWORD"),
        ("DASHBOARD_PASSWORD=pw-123\n", "DASHBOARD_USER"),
    ],
)
def test_half_configured_dashboard_auth_is_an_error(
    tmp_path: Path, body: str, missing: str
) -> None:
    env_file = write_env(tmp_path, body)
    with pytest.raises(ConfigError) as excinfo:
        dashboard_credentials(load_settings(env_file), env_file=env_file)
    message = str(excinfo.value)
    assert missing in message
    assert str(env_file) in message
    assert "pw-123" not in message


def test_dashboard_password_is_redacted_from_logs(tmp_path: Path) -> None:
    settings = load_settings(write_env(tmp_path, "DASHBOARD_PASSWORD=correct-horse\n"))
    assert "correct-horse" in settings.secret_values()


# --- run history retention ------------------------------------------------------------------


def test_run_history_is_kept_for_30_days_by_default(tmp_path: Path) -> None:
    assert load_legacy_config(write_config(tmp_path)).retention.runs_days == 30
    assert load_config(EXAMPLE_CONFIG).retention.runs_days == 30


def test_run_history_can_be_kept_forever(tmp_path: Path) -> None:
    config = load_legacy_config(write_config(tmp_path, "retention: {runs_days: null}\n"))
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
    config = load_legacy_config(
        write_config(tmp_path, "retention: {runs_days: 3}\nhealth: {enabled: false}\n")
    )
    assert config.retention.runs_days == 3


# --- Milestone 4 sections -------------------------------------------------------------


@pytest.mark.parametrize("wait", ["12", "20"])
def test_job_pages_wait_less_than_the_freshness_window(tmp_path: Path, wait: str) -> None:
    body = f"detection: {{freshness_hours: 12}}\nlinkedin: {{job_page_wait_hours: {wait}}}\n"
    message = config_error(write_config(tmp_path, body))
    assert f"linkedin.job_page_wait_hours ({wait}) must be less than" in message
    assert "detection.freshness_hours (12)" in message


def test_m4_sections_get_their_defaults(tmp_path: Path) -> None:
    config = load_legacy_config(write_config(tmp_path))
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


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("omega.wd5/External", "omega.wd5/External"),
        ("OMEGA.WD5/External", "omega.wd5/External"),
        ("https://omega.wd5.myworkdayjobs.com/en-US/External", "omega.wd5/External"),
        ("https://wd5.myworkdaysite.com/recruiting/omega/External", "omega.wd5/External"),
    ],
)
def test_workday_tokens_are_normalized(tmp_path: Path, token: str, expected: str) -> None:
    config = load_legacy_config(
        write_config(
            tmp_path,
            f"watchlist:\n  - {{name: Omega, ats: workday, token: '{token}', keywords: [backend]}}\n",
        )
    )
    [entry] = config.watchlist
    assert entry.token == expected
    assert entry.keywords == ["backend"]


@pytest.mark.parametrize(
    ("entry", "field", "hint"),
    [
        ("{name: Omega, ats: workday, token: omega}", "watchlist[0].token", "tenant.wdN/site"),
        ("{name: Acme, ats: greenhouse, token: 'acme/jobs'}", "watchlist[0].token", None),
        (
            "{name: Acme, ats: greenhouse, token: acme, keywords: [backend]}",
            "watchlist[0].keywords",
            "Workday",
        ),
        (
            "{name: Omega, ats: workday, token: omega.wd5/External, keywords: [backend, ' ']}",
            "watchlist[0].keywords",
            None,
        ),
    ],
)
def test_bad_workday_entries_name_the_field(
    tmp_path: Path, entry: str, field: str, hint: str | None
) -> None:
    path = write_config(tmp_path, f"watchlist:\n  - {entry}\n")
    message = config_error(path)
    assert field in message
    assert str(path) in message
    if hint is not None:
        assert hint in message


def test_the_same_workday_board_twice_is_a_duplicate(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        watchlist:
          - {name: Omega, ats: workday, token: omega.wd5/External}
          - {name: Omega again, ats: workday, token: 'https://omega.wd5.myworkdayjobs.com/External'}
        """,
    )
    assert "watchlist[1] is a duplicate of watchlist[0]" in config_error(path)


def test_a_workday_board_can_be_blocked(tmp_path: Path) -> None:
    config = load_legacy_config(
        write_config(tmp_path, "blocklist: {boards: [workday/omega.wd5/External]}\n")
    )
    assert company_blocklist(config).blocks_board("workday", "omega.wd5/External")


def test_workday_entries_no_longer_warn(tmp_path: Path) -> None:
    config = load_legacy_config(
        write_config(
            tmp_path, "watchlist:\n  - {name: Omega, ats: workday, token: omega.wd5/External}\n"
        )
    )
    assert not any("workday" in warning.lower() for warning in config_warnings(config))


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
    assert str(path) in message


def llm_config(tmp_path: Path) -> Path:
    return write_config(tmp_path, "notify: {channels: []}\nllm: {enabled: true}\n")


def test_the_llm_key_is_required_by_commands_that_call_the_llm(tmp_path: Path) -> None:
    config = load_legacy_config(llm_config(tmp_path))
    env = write_env(tmp_path)
    with pytest.raises(ConfigError) as excinfo:
        require_secrets(config, env_secrets(load_settings(env)), env_file=env, llm=True)
    assert "LLM_API_KEY" in str(excinfo.value)
    assert str(env) in str(excinfo.value)
    require_secrets(config, env_secrets(load_settings(env)), env_file=env)  # notify test: no LLM


def test_a_set_llm_key_passes_and_is_never_echoed(tmp_path: Path) -> None:
    config = load_legacy_config(llm_config(tmp_path))
    env = write_env(tmp_path, "LLM_API_KEY=sk-ant-secret-0123456789\n")
    settings = load_settings(env)
    require_secrets(config, env_secrets(settings), env_file=env, llm=True)
    assert "sk-ant-secret-0123456789" in settings.secret_values()


def test_the_llm_key_is_not_needed_while_the_llm_is_off(tmp_path: Path) -> None:
    path = write_config(tmp_path, "notify: {channels: []}\n")
    env = write_env(tmp_path)
    require_secrets(
        load_legacy_config(path), env_secrets(load_settings(env)), env_file=env, llm=True
    )


# --- M6: config.yaml holds infrastructure; preferences are the database profile ----------


def test_config_yaml_holds_infrastructure_only(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("detection: {freshness_hours: 24}\ndashboard: {port: 8100}\n", "utf-8")
    config = load_config(path)
    assert isinstance(config, InfraConfig)
    assert config.detection.freshness_hours == 24
    assert config.dashboard.port == 8100


def test_each_preference_key_left_in_config_yaml_is_its_own_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "contact_email: me@example.com\nfilters: {title: {include: [backend]}}\n"
        "watchlist: []\nnotify: {channels: [ntfy]}\ndashboard: {port: 0}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    fields = {error.field: error.message for error in excinfo.value.errors}
    assert set(fields) == {"contact_email", "filters", "watchlist", "notify", "dashboard.port"}
    assert "Settings" in fields["filters"]
    assert "profile import" in fields["filters"]
    assert "Watchlist page" in fields["watchlist"]
    assert "me@example.com" not in str(excinfo.value)  # the value is never echoed


def test_a_missing_config_yaml_means_defaults_only_when_allowed(tmp_path: Path) -> None:
    assert load_config(tmp_path / "config.yaml", missing_ok=True) == InfraConfig()
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "config.yaml")


def test_an_old_config_yaml_is_read_leniently_for_the_database_url(tmp_path: Path) -> None:
    path = write_config(tmp_path, "database_url: sqlite:///old.db\nfilters: {nonsense: 1}\n")
    assert read_config_lenient(path).database_url == "sqlite:///old.db"
    assert read_config_lenient(tmp_path / "missing.yaml") == InfraConfig()


def test_compose_and_split_are_inverses(tmp_path: Path) -> None:
    legacy = load_legacy_config(
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
