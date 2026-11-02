"""The board seed: the boards every install starts from, its daily remote sync, and the
boards you add yourself, ready to contribute back (M7).

The seed ships in the package (``data/seed/boards.json``): a ``version``, the ``boards``,
one per line and sorted, and ``removed`` tombstones for boards that died or turned out to be
staffing agencies. Agencies aren't in it; they ship as their own list
(``data/seed/agencies.json``), which ``hide_agencies`` adds to your blocklist.

Applying it (``jobpulse companies seed apply``) adds every board you don't have yet, as a
``seed`` company with a pending baseline: its first poll stores the jobs it has without
notifying you. Known, blocked and paused boards are skipped, so applying twice changes
nothing. A tombstone pauses a seed board only while nobody edited it (still active, at
priority 0). Workday boards are searched with ``SEED_WORKDAY_KEYWORDS``, each kept only
when it fits what a poll reads (``board_import.check_workday_keywords``); M8 derives them
from your roles.

With ``seed.remote`` on, a watchlist cycle fetches the public seed once a day with its
ETag. A body bigger than ``seed.max_bytes`` or that doesn't parse applies nothing; at most
``seed.max_new_boards`` new boards are validated per sync (the rest wait for the next day),
and each must answer its API before it's added. The ETag is saved in the same commit as the
boards, and each sync is a ``seed`` run on the Health page.

``jobpulse companies seed export`` builds the file from your watchlist: active boards, less
agencies and boards whose latest run was a 4xx, without priorities or keywords, plus boards
named with ``--add``, each validated once. Boards already in the seed stay unless they died
or are agencies; those become tombstones. ``jobpulse companies contribute`` writes the boards
you added that the seed lacks.
"""

import asyncio
import json
import logging
import re
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy.orm import Session, sessionmaker

from jobpulse.board_import import check_workday_keywords
from jobpulse.config import BOARD_REF_PATTERN, AppConfig, SeedConfig
from jobpulse.core.atsurls import AtsRef, default_board_name
from jobpulse.core.filters import Blocklist, FilterRules
from jobpulse.core.models import AtsName, Region
from jobpulse.core.timeparse import utcnow
from jobpulse.data import SEED_FILE, agency_names, read_text
from jobpulse.db import repo
from jobpulse.httpclient import HttpClient
from jobpulse.sources.ats_detect import AtsDetector
from jobpulse.sources.base import FetchError, HttpAdapter
from jobpulse.sources.workday import PAGE_LIMIT

log = logging.getLogger(__name__)

SEED_VERSION = 1
SOURCE = "seed"  # companies.source_of_discovery of seed boards, and runs.source of a sync
REMOTE_TARGET = "remote"  # runs.target of the remote sync
SYNC_EVERY = timedelta(days=1)
CONCURRENCY = 8  # validations at once (per-host limits still apply)
MAX_NAME_CHARS = 200
# Until M8 derives keywords from your roles: the set most of the imported boards kept.
SEED_WORKDAY_KEYWORDS = (
    "software engineer",
    "full stack",
    "backend engineer",
    "machine learning engineer",
)
ATS_NAMES: frozenset[str] = frozenset({"greenhouse", "lever", "ashby", "workday"})
# Flags names for review when the seed is exported; it never hides anything by itself.
_AGENCY_HINT = re.compile(
    r"\b(staffing|recruit(?:ing|ers?|ment)?|talents?|placements?|solutions partners?)\b",
    re.IGNORECASE,
)

Key = tuple[str, str]  # (ats, lowercase token)


class SeedError(ValueError):
    """The seed file is malformed: nothing in it is applied."""


@dataclass(frozen=True, slots=True)
class SeedBoard:
    ats: AtsName
    token: str
    name: str
    region: Region = "us"

    @property
    def key(self) -> Key:
        return (self.ats, self.token.lower())

    @property
    def ref(self) -> AtsRef:
        return AtsRef(self.ats, self.token, None, self.region)

    @property
    def label(self) -> str:
        return f"{self.ats}/{self.token}"


@dataclass(frozen=True, slots=True)
class Seed:
    boards: tuple[SeedBoard, ...] = ()
    removed: tuple[Key, ...] = ()  # tombstones

    def keys(self) -> set[Key]:
        return {board.key for board in self.boards}


# --- the file ---------------------------------------------------------------------------------


def _token(item: dict[str, Any], where: str) -> tuple[AtsName, str]:
    ats, token = item.get("ats"), item.get("token")
    if not isinstance(ats, str) or ats not in ATS_NAMES:
        raise SeedError(f"{where}.ats must be one of {', '.join(sorted(ATS_NAMES))}")
    if (
        not isinstance(token, str)
        or len(token) > 255
        or not BOARD_REF_PATTERN.fullmatch(f"{ats}/{token}")
    ):
        raise SeedError(f"{where}.token isn't a {ats} board token: {token!r}")
    return cast(AtsName, ats), token


def _board(item: Any, where: str) -> SeedBoard:
    if not isinstance(item, dict):
        raise SeedError(f"{where} must be an object")
    ats, token = _token(item, where)
    name = item.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_CHARS:
        raise SeedError(f"{where}.name must be a company name of 1 to {MAX_NAME_CHARS} characters")
    region = item.get("region", "us")
    if region not in ("us", "eu"):
        raise SeedError(f"{where}.region must be us or eu")
    return SeedBoard(ats, token, " ".join(name.split()), cast(Region, region))


def parse_seed(text: str) -> Seed:
    """Read a seed file, or raise ``SeedError`` naming the first problem."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SeedError(f"not JSON: {exc}") from None
    if not isinstance(data, dict) or data.get("version") != SEED_VERSION:
        raise SeedError(f"expected an object with version {SEED_VERSION}")
    boards, removed = data.get("boards"), data.get("removed", [])
    if not isinstance(boards, list) or not isinstance(removed, list):
        raise SeedError("boards and removed must be lists")
    parsed: dict[Key, SeedBoard] = {}
    for index, item in enumerate(boards):
        board = _board(item, f"boards[{index}]")
        if board.key in parsed:
            raise SeedError(f"boards[{index}] repeats {board.label}")
        parsed[board.key] = board
    tombstones: dict[Key, None] = {}
    for index, item in enumerate(removed):
        if not isinstance(item, dict):
            raise SeedError(f"removed[{index}] must be an object")
        ats, token = _token(item, f"removed[{index}]")
        key = (ats, token.lower())
        if key in parsed:
            raise SeedError(f"removed[{index}] ({ats}/{token}) is also in boards")
        tombstones[key] = None
    return Seed(tuple(parsed.values()), tuple(tombstones))


def render_seed(seed: Seed) -> str:
    """The file: boards and tombstones sorted by board, one per line."""

    def entry(board: SeedBoard) -> str:
        value: dict[str, str] = {"ats": board.ats, "token": board.token, "name": board.name}
        if board.region != "us":
            value["region"] = board.region
        return json.dumps(value, ensure_ascii=False)

    def block(lines: list[str]) -> str:
        return "[]" if not lines else "[\n" + ",\n".join(f"    {line}" for line in lines) + "\n  ]"

    boards = [entry(b) for b in sorted(seed.boards, key=lambda b: b.key)]
    removed = [json.dumps({"ats": ats, "token": token}) for ats, token in sorted(set(seed.removed))]
    return (
        f'{{\n  "version": {SEED_VERSION},\n  "boards": {block(boards)},\n'
        f'  "removed": {block(removed)}\n}}\n'
    )


def packaged_seed() -> Seed:
    return parse_seed(read_text(SEED_FILE))


def agency_blocklist() -> Blocklist:
    return Blocklist.build(companies=agency_names())


def looks_like_agency(name: str) -> bool:
    """A name worth a second look before it goes in the seed (staffing, recruiting, talent,
    placements)."""
    return bool(_AGENCY_HINT.search(name))


# --- applying ---------------------------------------------------------------------------------

Outcome = Literal["add", "blocked", "invalid", "too broad"]


@dataclass(frozen=True, slots=True)
class SeedResult:
    board: SeedBoard
    outcome: Outcome
    keywords: tuple[str, ...] = ()  # Workday: the keywords kept
    detail: str = ""


@dataclass
class SeedPlan:
    results: list[SeedResult] = field(default_factory=list)
    known: int = 0  # boards already stored (active or paused)
    capped: int = 0  # new boards left for a later sync (seed.max_new_boards)
    removed: tuple[Key, ...] = ()

    @property
    def add(self) -> list[SeedResult]:
        return [result for result in self.results if result.outcome == "add"]


async def _check(
    board: SeedBoard,
    *,
    detector: AtsDetector | None,
    blocklist: Blocklist,
    config: AppConfig,
    rules: FilterRules,
    validate: bool,
) -> SeedResult:
    if validate or board.ats == "workday":
        if detector is None:
            return SeedResult(board, "invalid", detail="no network to check it")
        if validate:
            validation = await detector.validate(board.ref)
            if not validation.ok:
                return SeedResult(board, "invalid", detail=validation.error or "")
            board = replace(board, region=validation.ref.region)
            if validation.name and blocklist.blocks_company(validation.name):
                return SeedResult(board, "blocked", detail=f"{validation.name} is blocked")
    if board.ats != "workday" or detector is None:
        return SeedResult(board, "add")
    check = await check_workday_keywords(
        detector,
        board.ref,
        SEED_WORKDAY_KEYWORDS,
        cap=config.workday.max_pages * PAGE_LIMIT,
        rules=rules,
    )
    if check.error is not None:
        return SeedResult(board, "invalid", detail=check.error)
    if not check.kept:
        return SeedResult(board, "too broad", detail="no keyword fits what a poll reads")
    return SeedResult(board, "add", tuple(keyword for keyword, _ in check.kept))


async def plan_seed(
    seed: Seed,
    *,
    known: Collection[Key],
    blocklist: Blocklist,
    config: AppConfig,
    rules: FilterRules,
    detector: AtsDetector | None,
    validate: bool = False,
    max_new: int | None = None,
) -> SeedPlan:
    """Which of the seed's boards to add. The packaged seed was validated when it was built,
    so only Workday boards are searched (for their keywords); a remote seed is ``validate``d
    board by board."""
    plan = SeedPlan(removed=seed.removed)
    pending: list[SeedBoard] = []
    for board in seed.boards:
        if board.key in known:
            plan.known += 1
        elif blocklist.blocks_board(*board.key) or blocklist.blocks_company(board.name):
            plan.results.append(SeedResult(board, "blocked", detail="on your blocklist"))
        else:
            pending.append(board)
    if max_new is not None and len(pending) > max_new:
        plan.capped = len(pending) - max_new
        pending = pending[:max_new]
    gate = asyncio.Semaphore(CONCURRENCY)

    async def check(board: SeedBoard) -> SeedResult:
        async with gate:
            return await _check(
                board,
                detector=detector,
                blocklist=blocklist,
                config=config,
                rules=rules,
                validate=validate,
            )

    plan.results += await asyncio.gather(*(check(board) for board in pending))
    return plan


def write_seed(session: Session, plan: SeedPlan, *, now: datetime) -> tuple[list[str], list[str]]:
    """Add the plan's boards and pause the tombstoned seed boards nobody edited; the labels
    of both."""
    added: list[str] = []
    for result in plan.add:
        board = result.board
        if repo.add_company(
            session,
            ats=board.ats,
            token=board.token,
            region=board.region,
            name=board.name,
            source=SOURCE,
            priority=0,
            discovered_from=None,
            now=now,
            params={"keywords": list(result.keywords)} if result.keywords else None,
        ):
            added.append(board.label)
    retired: list[str] = []
    for company in repo.retirable_seed_companies(session, set(plan.removed)):
        company.active = False
        retired.append(f"{company.ats_type}/{company.token}")
    session.flush()
    return added, retired


def retirable(session: Session, plan: SeedPlan) -> list[str]:
    """What ``write_seed`` would pause (a dry run)."""
    return [
        f"{company.ats_type}/{company.token}"
        for company in repo.retirable_seed_companies(session, set(plan.removed))
    ]


# --- the remote sync --------------------------------------------------------------------------


@dataclass
class SyncOutcome:
    not_modified: bool = False
    boards: int = 0  # in the fetched seed
    added: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # "label: why", invalid or too broad
    capped: int = 0
    problem: str | None = None  # the fetch or the file failed: nothing applied


class SeedSync:
    """Fetches the public seed once a day and adds its new boards (``seed.remote``)."""

    def __init__(
        self,
        http: HttpClient,
        detector: AtsDetector,
        settings: SeedConfig,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._http = HttpAdapter(http)
        self._detector = detector
        self._settings = settings
        self._clock = clock
        self._next: datetime | None = None  # in memory, like the run pruning

    def due(self, now: datetime) -> bool:
        return self._next is None or now >= self._next

    async def run(
        self,
        session: Session,
        checkpoint: Callable[[], None],
        *,
        config: AppConfig,
        blocklist: Blocklist,
        rules: FilterRules,
    ) -> SyncOutcome:
        """One sync. ``checkpoint`` commits: nothing stays open while the network runs."""
        settings = self._settings
        started = self._clock()
        self._next = started + SYNC_EVERY
        outcome = SyncOutcome()
        state = repo.seed_state(session)
        etag = state.etag if state is not None and state.url == settings.url else None
        known = repo.known_companies(session)
        checkpoint()
        try:
            response = await self._http.get_response(
                settings.url,
                headers={"If-None-Match": etag} if etag else None,
                accept=(200, 304),
            )
            if response.status_code == 304:
                if etag is None:
                    raise FetchError("other", "HTTP 304 although no ETag was sent", status=304)
                outcome.not_modified = True
            elif len(response.content) > settings.max_bytes:
                raise FetchError(
                    "parse",
                    f"{len(response.content):,} bytes, more than seed.max_bytes "
                    f"({settings.max_bytes:,})",
                )
            else:
                seed = parse_seed(response.text)
        except (FetchError, SeedError) as exc:
            kind = exc.kind if isinstance(exc, FetchError) else "parse"
            outcome.problem = exc.message if isinstance(exc, FetchError) else str(exc)
            log.warning("seed: sync failed (%s): %s", kind, outcome.problem)
            repo.record_run(
                session,
                source=SOURCE,
                target=REMOTE_TARGET,
                started_at=started,
                finished_at=self._clock(),
                ok=False,
                error=outcome.problem,
                error_kind=kind,
            )
            checkpoint()
            return outcome
        if outcome.not_modified:
            now = self._clock()
            repo.save_seed_state(session, url=settings.url, etag=etag, checked_at=now)
            repo.record_run(
                session,
                source=SOURCE,
                target=REMOTE_TARGET,
                started_at=started,
                finished_at=now,
                ok=True,
                not_modified=True,
            )
            checkpoint()
            return outcome

        plan = await plan_seed(
            seed,
            known=known,
            blocklist=blocklist,
            config=config,
            rules=rules,
            detector=self._detector,
            validate=True,
            max_new=settings.max_new_boards,
        )
        now = self._clock()
        outcome.boards = len(seed.boards)
        outcome.added, outcome.retired = write_seed(session, plan, now=now)
        outcome.skipped = [
            f"{result.board.label}: {result.outcome}"
            + (f" ({result.detail})" if result.detail else "")
            for result in plan.results
            if result.outcome in ("invalid", "too broad")
        ]
        outcome.capped = plan.capped
        # With boards left over, no ETag: tomorrow's fetch brings the file again.
        repo.save_seed_state(
            session,
            url=settings.url,
            etag=None if plan.capped else response.headers.get("ETag"),
            checked_at=now,
            synced_at=now,
        )
        repo.record_run(
            session,
            source=SOURCE,
            target=REMOTE_TARGET,
            started_at=started,
            finished_at=now,
            ok=True,
            fetched=len(seed.boards),
            new=len(outcome.added),
            stats={
                "added": len(outcome.added),
                "retired": len(outcome.retired),
                "skipped": len(outcome.skipped),
                "capped": plan.capped,
            },
        )
        checkpoint()
        for label in outcome.added:
            log.info("seed: added %s", label)
        return outcome


# --- building the file ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtraBoard:
    """A board named with ``--add ats/token[=Name]``."""

    ref: AtsRef
    name: str | None = None


def parse_extra(value: str) -> ExtraBoard:
    board, _, name = value.partition("=")
    ats, _, token = board.strip().partition("/")
    ats = ats.lower()
    if ats not in ATS_NAMES or not BOARD_REF_PATTERN.fullmatch(f"{ats}/{token}"):
        raise ValueError(f"{value!r} is not ats/token[=Name], e.g. greenhouse/anthropic=Anthropic")
    clean = " ".join(name.split()) or None
    return ExtraBoard(AtsRef(cast(AtsName, ats), token), clean)


@dataclass
class ExportReport:
    seed: Seed
    agencies: list[str] = field(default_factory=list)  # left out: staffing agencies
    dead: list[str] = field(default_factory=list)  # left out: the latest run was a 4xx
    carried: int = 0  # kept from the previous seed though not active here
    extra_added: list[str] = field(default_factory=list)
    extra_known: list[str] = field(default_factory=list)
    extra_invalid: list[str] = field(default_factory=list)  # "label: why"
    flagged: list[str] = field(default_factory=list)  # names that look like agencies


def _labels(boards: Iterable[SeedBoard]) -> list[str]:
    return sorted(f"{b.label} ({b.name})" for b in boards)


async def export_seed(
    *,
    sessions: sessionmaker[Session],
    previous: Seed,
    extra: Sequence[ExtraBoard] = (),
    detector: AtsDetector | None = None,
) -> ExportReport:
    """The seed built from the watchlist, on top of ``previous``."""
    agencies = agency_blocklist()
    with sessions() as session:
        rows = repo.board_rows(session)
        dead = repo.failing_boards(session)
    boards: dict[Key, SeedBoard] = {}
    left_out: dict[Key, SeedBoard] = {}
    report = ExportReport(seed=previous)
    local: dict[Key, SeedBoard] = {}
    for row in rows:
        board = SeedBoard(
            cast(AtsName, row.ats_type), row.token, row.name, cast(Region, row.region)
        )
        if row.active:
            local[board.key] = board
        if board.key in dead:
            left_out[board.key] = board
            report.dead.append(f"{board.label} ({board.name})")
    for board in (*previous.boards, *local.values()):
        if board.key in left_out:
            continue
        if agencies.blocks_company(board.name):
            left_out[board.key] = board
            report.agencies.append(f"{board.label} ({board.name})")
            continue
        boards[board.key] = local.get(board.key, board)
    report.carried = sum(1 for board in previous.boards if board.key not in local)

    for wanted in extra:
        key = (wanted.ref.ats, wanted.ref.token.lower())
        label = f"{wanted.ref.ats}/{wanted.ref.token}"
        if key in boards:
            report.extra_known.append(label)
            continue
        if detector is None:
            report.extra_invalid.append(f"{label}: no network to validate it")
            continue
        validation = await detector.validate(wanted.ref)
        if not validation.ok:
            report.extra_invalid.append(f"{label}: {validation.error}")
            continue
        name = wanted.name or validation.name or default_board_name(validation.ref)
        board = SeedBoard(wanted.ref.ats, wanted.ref.token, name, validation.ref.region)
        if agencies.blocks_company(name):
            report.agencies.append(f"{board.label} ({name})")
            continue
        boards[key] = board
        left_out.pop(key, None)
        report.extra_added.append(f"{board.label} ({name})")

    removed = (set(previous.removed) | (previous.keys() & set(left_out))) - set(boards)
    report.seed = Seed(tuple(boards.values()), tuple(sorted(removed)))
    report.flagged = _labels(b for b in boards.values() if looks_like_agency(b.name))
    report.agencies.sort()
    report.dead.sort()
    return report


def contribution(session: Session, seed: Seed) -> Seed:
    """Your active boards the seed lacks: what you added yourself, less agencies."""
    agencies = agency_blocklist()
    known = seed.keys() | set(seed.removed)
    boards = [
        SeedBoard(cast(AtsName, row.ats_type), row.token, row.name, cast(Region, row.region))
        for row in repo.board_rows(session)
        if row.active
        and row.source_of_discovery != SOURCE
        and (row.ats_type, row.token.lower()) not in known
        and not agencies.blocks_company(row.name)
    ]
    return Seed(tuple(boards))
