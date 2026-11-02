"""The files the package ships (M7): the seed is valid, sorted, unique and free of staffing
agencies, and both load from the installed package. Run this after editing either file."""

import json
from importlib.resources import files

from jobpulse.core.dedupe import normalize_company
from jobpulse.data import AGENCIES_FILE, SEED_FILE, agency_names, read_text
from jobpulse.seed import agency_blocklist, packaged_seed, parse_seed, render_seed


def test_the_seed_loads_from_the_package() -> None:
    resource = files("jobpulse").joinpath("data", SEED_FILE)
    assert resource.is_file()
    assert parse_seed(resource.read_text(encoding="utf-8")) == packaged_seed()


def test_the_seed_is_written_the_way_render_seed_writes_it() -> None:
    """Sorted by board, one per line, no repeats (parse_seed refuses those)."""
    text = read_text(SEED_FILE)
    assert render_seed(parse_seed(text)) == text


def test_the_seed_holds_no_staffing_agency() -> None:
    agencies = agency_blocklist()
    assert [b.label for b in packaged_seed().boards if agencies.blocks_company(b.name)] == []


def test_the_agency_list_is_sorted_and_unique() -> None:
    data = json.loads(read_text(AGENCIES_FILE))
    assert data["version"] == 1
    names = data["agencies"]
    assert names == sorted(names, key=str.casefold)
    keys = [normalize_company(name) for name in names]
    assert all(keys)
    assert len(set(keys)) == len(keys)
    assert agency_names() == tuple(names)
