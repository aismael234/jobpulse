"""Check a change to the board seed (CI runs this on pull requests that edit it): the file is
written the way ``render_seed`` writes it, and every board it adds answers on its ATS and isn't
a staffing agency. Boards already in the base are not requested again.

    uv run python -m jobpulse.devtools.seed_check --base OLD.json [--head boards.json]
"""

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from jobpulse.config import InfraConfig
from jobpulse.data import SEED_FILE
from jobpulse.runtime import build_http
from jobpulse.seed import (
    CONCURRENCY,
    Seed,
    SeedBoard,
    SeedError,
    agency_blocklist,
    looks_like_agency,
    parse_seed,
    render_seed,
)
from jobpulse.sources.ats_detect import AtsDetector

HEAD = Path("src/jobpulse/data") / SEED_FILE


def read_seed(path: Path) -> Seed:
    """The seed at ``path``; a missing file (a branch from before the seed) is empty."""
    try:
        return parse_seed(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Seed()


async def problems(base: Seed, head: Seed, detector: AtsDetector) -> list[tuple[str, str]]:
    """(label, problem) for each board ``head`` adds that fails, in the seed's order."""
    known = base.keys()
    added = [board for board in head.boards if board.key not in known]
    agencies = agency_blocklist()
    gate = asyncio.Semaphore(CONCURRENCY)

    async def check(board: SeedBoard) -> str | None:
        if agencies.blocks_company(board.name):
            return f"{board.name} is on the staffing agency list"
        if looks_like_agency(board.name):
            return f"{board.name} looks like a staffing agency; leave it out of the seed"
        async with gate:
            validation = await detector.validate(board.ref)
        return None if validation.ok else f"doesn't answer: {validation.error or 'unknown error'}"

    found = await asyncio.gather(*(check(board) for board in added))
    return [
        (board.label, problem)
        for board, problem in zip(added, found, strict=True)
        if problem is not None
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="seed_check", description=__doc__.split("\n")[0])
    parser.add_argument("--base", type=Path, required=True, help="the seed before the change")
    parser.add_argument("--head", type=Path, default=HEAD, help="the seed after it")
    args = parser.parse_args(argv)

    try:
        base, head = read_seed(args.base), read_seed(args.head)
    except SeedError as exc:
        print(f"the seed is malformed: {exc}")
        return 1
    failed = False
    if render_seed(head) != args.head.read_text(encoding="utf-8"):
        print(
            f"{args.head} isn't written the way jobpulse writes it: regenerate it with "
            "`jobpulse companies seed export`"
        )
        failed = True
    added = len(head.keys() - base.keys())
    if not added:
        print("no boards added")
        return 1 if failed else 0

    async def run() -> list[tuple[str, str]]:
        async with build_http(InfraConfig()) as http:
            return await problems(base, head, AtsDetector(http))

    found = asyncio.run(run())
    for label, problem in found:
        print(f"{label}: {problem}")
    print(f"{added} boards added, {len(found)} with problems")
    return 1 if failed or found else 0


if __name__ == "__main__":
    raise SystemExit(main())
