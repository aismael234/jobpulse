"""The CI check on a seed change: every board it adds answers on its ATS, isn't a staffing
agency, and the file is written the way render_seed writes it."""

from pathlib import Path

import pytest
import respx

from jobpulse.devtools import seed_check
from jobpulse.httpclient import HttpClient
from jobpulse.seed import Seed, SeedBoard, render_seed
from jobpulse.sources.ats_detect import AtsDetector

GH = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"

BASE = Seed(boards=(SeedBoard("greenhouse", "acme", "Acme"),))


def with_boards(*boards: SeedBoard) -> Seed:
    return Seed(boards=tuple(sorted((*BASE.boards, *boards), key=lambda b: b.key)))


async def test_only_added_boards_are_checked_and_a_live_board_passes(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    route = respx_mock.get(GH.format(token="zeta")).respond(
        200, json={"jobs": [{"company_name": "Zeta"}]}
    )
    head = with_boards(SeedBoard("greenhouse", "zeta", "Zeta"))
    assert await seed_check.problems(BASE, head, AtsDetector(http)) == []
    assert route.call_count == 1  # acme was in the base: never requested


async def test_a_dead_board_and_an_agency_are_problems(
    respx_mock: respx.MockRouter, http: HttpClient
) -> None:
    respx_mock.get(ASHBY.format(token="gone")).respond(404, text="Not Found")
    head = with_boards(
        SeedBoard("ashby", "gone", "Gone"),
        SeedBoard("greenhouse", "cybercoders", "CyberCoders"),
    )
    found = await seed_check.problems(BASE, head, AtsDetector(http))
    assert [label for label, _ in found] == ["ashby/gone", "greenhouse/cybercoders"]
    assert "staffing agency" in found[1][1]


def test_main_reports_a_file_that_render_seed_would_write_differently(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = tmp_path / "base.json"
    base.write_text(render_seed(BASE), encoding="utf-8")
    head = tmp_path / "head.json"
    head.write_text(render_seed(BASE).replace("\n", "\n\n", 1), encoding="utf-8")
    assert seed_check.main(["--base", str(base), "--head", str(head)]) == 1
    assert "jobpulse companies seed export" in capsys.readouterr().out


def test_main_passes_when_nothing_was_added(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = tmp_path / "base.json"
    base.write_text(render_seed(BASE), encoding="utf-8")
    assert seed_check.main(["--base", str(base), "--head", str(base)]) == 0
    assert "no boards added" in capsys.readouterr().out


def test_a_missing_base_counts_as_an_empty_seed(tmp_path: Path) -> None:
    assert seed_check.read_seed(tmp_path / "missing.json") == Seed()
