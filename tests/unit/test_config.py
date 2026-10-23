"""Config validation: every error names the offending field and the file it came from."""

import textwrap
from pathlib import Path

import pytest

from jobpulse.config import (
    DEFAULT_DATABASE_URL,
    ConfigError,
    config_warnings,
    load_config,
    load_settings,
    require_secrets,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config.example.yaml"
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
        load_config(path)
    return str(excinfo.value)


def secrets_error(config_path: Path, env_path: Path) -> str:
    config = load_config(config_path)
    with pytest.raises(ConfigError) as excinfo:
        require_secrets(config, load_settings(env_path), env_file=env_path)
    return str(excinfo.value)


# --- config.yaml ------------------------------------------------------------------


def test_example_config_is_valid() -> None:
    config = load_config(EXAMPLE_CONFIG)
    assert {entry.ats for entry in config.watchlist} == {"greenhouse", "lever", "ashby"}
    assert config.notify.channels == ["ntfy"]


def test_minimal_config_gets_spec_defaults(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
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
    warnings = config_warnings(load_config(EXAMPLE_CONFIG))
    assert any("filters.level.include" in w and "<SET ME>" in w for w in warnings)
    assert any("contact_email" in w for w in warnings)


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
    config = load_config(write_config(tmp_path))
    env_file = write_env(tmp_path, f"NTFY_TOPIC={GOOD_TOPIC}\n")
    require_secrets(config, load_settings(env_file), env_file=env_file)


def test_environment_variable_satisfies_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NTFY_TOPIC", GOOD_TOPIC)
    config = load_config(write_config(tmp_path))
    env_file = write_env(tmp_path)
    require_secrets(config, load_settings(env_file), env_file=env_file)


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
    config = load_config(write_config(tmp_path, "notify: {channels: []}\n"))
    env_file = write_env(tmp_path)
    require_secrets(config, load_settings(env_file), env_file=env_file)


def test_settings_mask_secrets_in_repr(tmp_path: Path) -> None:
    settings = load_settings(write_env(tmp_path, f"NTFY_TOPIC={GOOD_TOPIC}\n"))
    assert GOOD_TOPIC not in repr(settings)
    assert settings.ntfy_topic is not None
    assert settings.ntfy_topic.get_secret_value() == GOOD_TOPIC


def test_empty_env_value_counts_as_unset(tmp_path: Path) -> None:
    settings = load_settings(write_env(tmp_path, "NTFY_TOPIC=\nDISCORD_WEBHOOK_URL=\n"))
    assert settings.ntfy_topic is None
    assert settings.discord_webhook_url is None
