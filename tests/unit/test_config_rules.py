from pathlib import Path

from jobpulse.config import (
    AppConfig,
    Profile,
    filter_rules,
    location_aliases,
    parse_profile_yaml,
    scoring_weights,
)
from jobpulse.core.filters import check_title
from jobpulse.core.scoring import Weights

EXAMPLE_PROFILE = Path(__file__).resolve().parents[2] / "profile.example.yaml"


def example_profile() -> Profile:
    return parse_profile_yaml(EXAMPLE_PROFILE.read_text(encoding="utf-8"))


def test_rules_compile_from_the_example_profile() -> None:
    rules = filter_rules(example_profile())
    assert check_title("Backend Engineer", rules).passed
    assert check_title("BACKEND ENGINEER", rules).passed
    assert not check_title("Staff Backend Engineer", rules).passed
    assert not check_title("Engineering Manager", rules).passed


def test_placeholders_are_dropped_when_compiling() -> None:
    rules = filter_rules(example_profile())
    assert rules.level_include == ()


def test_location_aliases_merge_user_entries_over_defaults() -> None:
    config = AppConfig.model_validate(
        {
            "contact_email": "dev@example.com",
            "filters": {"locations": {"aliases": {"HQ": "Austin, TX"}}},
        }
    )
    aliases = location_aliases(config)
    assert aliases["hq"] == "Austin, TX"
    assert aliases["sf"] == "San Francisco, CA"


def test_location_rules_compile() -> None:
    config = AppConfig.model_validate(
        {
            "contact_email": "dev@example.com",
            "filters": {"locations": {"include": ["remote"], "unknown_location_passes": False}},
        }
    )
    rules = filter_rules(config).locations
    assert rules.include[0].search("REMOTE (US)")
    assert rules.unknown_passes is False


def test_countries_compile_to_iso_codes() -> None:
    config = AppConfig.model_validate(
        {
            "contact_email": "dev@example.com",
            "filters": {"locations": {"countries": ["us", "United States", "Canada", "UK"]}},
        }
    )
    assert config.filters.locations.countries == ["US", "CA", "GB"]
    assert filter_rules(config).locations.countries == frozenset({"US", "CA", "GB"})


def test_no_countries_by_default() -> None:
    config = AppConfig.model_validate({"contact_email": "dev@example.com"})
    assert filter_rules(config).locations.countries == frozenset()


def test_scoring_weights_come_from_config() -> None:
    config = AppConfig.model_validate(
        {"contact_email": "dev@example.com", "scoring": {"weights": {"title": 0.9}}}
    )
    weights = scoring_weights(config)
    assert weights == Weights(title=0.9)
