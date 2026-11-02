"""The seed file (M7): read strictly, written sorted with one board per line."""

import json
import re
from typing import Any

import pytest

from jobpulse.core.atsurls import AtsRef
from jobpulse.seed import (
    ExtraBoard,
    Seed,
    SeedBoard,
    SeedError,
    looks_like_agency,
    parse_extra,
    parse_seed,
    render_seed,
)


def seed_text(boards: list[Any], removed: list[Any] | None = None, version: Any = 1) -> str:
    return json.dumps({"version": version, "boards": boards, "removed": removed or []})


def test_boards_and_tombstones_round_trip_sorted_one_per_line() -> None:
    seed = Seed(
        (
            SeedBoard("lever", "olx", "OLX", "eu"),
            SeedBoard("ashby", "Linear", "Linear"),
            SeedBoard("workday", "nvidia.wd5/NVIDIAExternalCareerSite", "NVIDIA"),
            SeedBoard("ashby", "clerk", "Clerk"),
        ),
        (("greenhouse", "gone"), ("ashby", "dead")),
    )
    text = render_seed(seed)
    assert text == (
        "{\n"
        '  "version": 1,\n'
        '  "boards": [\n'
        '    {"ats": "ashby", "token": "clerk", "name": "Clerk"},\n'
        '    {"ats": "ashby", "token": "Linear", "name": "Linear"},\n'
        '    {"ats": "lever", "token": "olx", "name": "OLX", "region": "eu"},\n'
        '    {"ats": "workday", "token": "nvidia.wd5/NVIDIAExternalCareerSite", "name": "NVIDIA"}\n'
        "  ],\n"
        '  "removed": [\n'
        '    {"ats": "ashby", "token": "dead"},\n'
        '    {"ats": "greenhouse", "token": "gone"}\n'
        "  ]\n"
        "}\n"
    )
    parsed = parse_seed(text)
    assert set(parsed.boards) == set(seed.boards)
    assert set(parsed.removed) == set(seed.removed)
    assert render_seed(parsed) == text


def test_an_empty_seed_renders_empty_lists() -> None:
    assert render_seed(Seed()) == '{\n  "version": 1,\n  "boards": [],\n  "removed": []\n}\n'
    assert parse_seed(render_seed(Seed())) == Seed()


def test_names_are_tidied_and_keys_ignore_token_case() -> None:
    seed = parse_seed(seed_text([{"ats": "greenhouse", "token": "Stripe", "name": " Stripe\n "}]))
    (board,) = seed.boards
    assert board.name == "Stripe"
    assert board.key == ("greenhouse", "stripe")
    assert board.ref == AtsRef("greenhouse", "Stripe")


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("not json", "not JSON"),
        (seed_text([], version=2), "version 1"),
        (json.dumps({"version": 1, "boards": {}}), "must be lists"),
        (seed_text(["greenhouse/acme"]), "boards[0] must be an object"),
        (seed_text([{"ats": "workable", "token": "acme", "name": "A"}]), "boards[0].ats"),
        (seed_text([{"ats": "greenhouse", "token": "../x", "name": "A"}]), "boards[0].token"),
        (seed_text([{"ats": "workday", "token": "nvidia", "name": "N"}]), "boards[0].token"),
        (seed_text([{"ats": "greenhouse", "token": "acme", "name": " "}]), "boards[0].name"),
        (seed_text([{"ats": "greenhouse", "token": "acme", "name": "A" * 201}]), "boards[0].name"),
        (
            seed_text([{"ats": "lever", "token": "olx", "name": "OLX", "region": "apac"}]),
            "boards[0].region",
        ),
        (
            seed_text(
                [
                    {"ats": "greenhouse", "token": "acme", "name": "A"},
                    {"ats": "greenhouse", "token": "ACME", "name": "A"},
                ]
            ),
            "boards[1] repeats greenhouse/ACME",
        ),
        (
            seed_text(
                [{"ats": "greenhouse", "token": "acme", "name": "A"}],
                [{"ats": "greenhouse", "token": "Acme"}],
            ),
            "removed[0] (greenhouse/Acme) is also in boards",
        ),
        (seed_text([], [{"ats": "greenhouse"}]), "removed[0].token"),
    ],
)
def test_a_malformed_seed_names_its_first_problem(text: str, problem: str) -> None:
    with pytest.raises(SeedError, match=re.escape(problem)):
        parse_seed(text)


def test_boards_to_add_are_named_ats_slash_token_with_an_optional_name() -> None:
    assert parse_extra("greenhouse/anthropic=Anthropic") == ExtraBoard(
        AtsRef("greenhouse", "anthropic"), "Anthropic"
    )
    assert parse_extra("Lever/palantir") == ExtraBoard(AtsRef("lever", "palantir"), None)
    assert parse_extra("workday/ngc.wd1/Northrop_Grumman_External_Site=Northrop Grumman") == (
        ExtraBoard(AtsRef("workday", "ngc.wd1/Northrop_Grumman_External_Site"), "Northrop Grumman")
    )
    for bad in ("anthropic", "workable/acme", "greenhouse/", "workday/nvidia"):
        with pytest.raises(ValueError, match="ats/token"):
            parse_extra(bad)


@pytest.mark.parametrize(
    ("name", "flagged"),
    [
        ("Motion Recruitment", True),
        ("Recruiting from Scratch", True),
        ("Acme Staffing", True),
        ("Nova Talent", True),
        ("ExecutivePlacements", False),  # one word: the explicit list catches it
        ("Mural", False),
        ("Stripe", False),
    ],
)
def test_names_that_look_like_agencies_are_flagged_for_review(name: str, flagged: bool) -> None:
    assert looks_like_agency(name) is flagged
