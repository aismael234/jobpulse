"""M5a: how well resume similarity tracks Applied against Dismissed (development only).

Run it on a scratch copy of the database, never the live one::

    uv run python -m jobpulse.devtools.resume_eval --database-url sqlite:///<copy.db> \\
        --resume <resume.pdf> --out <scratch dir>

Labels are Applied (1) against Dismissed (0), postings with a description only. Dismissals
with another likely cause are set aside and reported as their own groups, never mixed into
the main comparison:

- ``rules``: today's compiled rules filter the posting (blocklist, title, location, years);
- ``pre-0005``: triaged before migration 0005, so no ``status_changed_at``;
- ``bulk``: dismissed in one bulk move (``status_changed_at`` shared with another row).

Every variant (posting text, posting chunking, resume side, aggregation) gets an AUC with a
bootstrap 95% CI against the kept dismissals, plus an AUC against each set-aside group. The
best variant is calibrated on recent matches (p5/p95) and dropped into the stored score in
place of the stack part at several weights.
"""

import argparse
import csv
import math
import random
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from itertools import product
from pathlib import Path
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpulse.core.filters import (
    FilterRules,
    check_description,
    check_locations,
    check_title,
)
from jobpulse.core.similarity import (
    AGGREGATIONS,
    Aggregation,
    RawMatch,
    Vector,
    anchors,
    calibrate,
    chunk_text,
    match,
    rescore_breakdown,
    resume_lines,
)
from jobpulse.db.tables import PostingRow
from jobpulse.embeddings.base import Embedder

GROUPS = ("rules", "pre-0005", "bulk")
SIMULATED_WEIGHTS = (0.1, 0.15, 0.2, 0.25, 0.3, 0.4)
_EMBED_BATCH = 64
_EXTREMES = 10
_LINE_CHARS = 100


type TextKind = Literal["title+desc", "desc"]
type PostingSide = Literal["block", "chunks"]
type ResumeSide = Literal["chunks", "lines"]
TEXT_KINDS: tuple[TextKind, ...] = ("title+desc", "desc")
POSTING_SIDES: tuple[PostingSide, ...] = ("block", "chunks")
RESUME_SIDES: tuple[ResumeSide, ...] = ("chunks", "lines")


@dataclass(frozen=True)
class Variant:
    text: TextKind
    posting: PostingSide
    resume: ResumeSide
    aggregation: Aggregation

    @property
    def name(self) -> str:
        return f"{self.text}|{self.posting}|{self.resume}|{self.aggregation}"


VARIANTS: tuple[Variant, ...] = tuple(
    Variant(text, posting, resume, aggregation)
    for text, posting, resume, aggregation in product(
        TEXT_KINDS, POSTING_SIDES, RESUME_SIDES, AGGREGATIONS
    )
)


@dataclass(frozen=True)
class Scored:
    """One posting with a description, as the analysis sees it."""

    id: int
    status: str
    group: str  # applied, kept, rules, pre-0005, bulk; "reference" for unlabeled matches
    set_aside: str | None  # why a dismissal was set aside
    rules_note: str | None  # an applied posting today's rules would filter
    company: str
    title: str
    description: str
    breakdown: Mapping[str, float | None] | None

    def part(self, name: str) -> float:
        value = (self.breakdown or {}).get(name)
        return float(value) if value is not None else 0.0


@dataclass(frozen=True)
class VariantResult:
    variant: Variant
    auc: float | None  # applied against kept dismissals
    ci: tuple[float, float] | None
    by_group: dict[str, float | None]  # applied against each set-aside group
    anchors: tuple[float, float]  # p5 and p95 of raw matches over recent matches


@dataclass(frozen=True)
class Baseline:
    name: str
    auc: float | None
    ci: tuple[float, float] | None
    by_group: dict[str, float | None]


@dataclass(frozen=True)
class Extreme:
    group: str
    title: str
    company: str
    raw: float
    line: str | None


@dataclass
class EvalReport:
    counts: Counter[str]
    without_description: Counter[str]
    applied_filtered_today: int
    reference_count: int
    baselines: list[Baseline]
    variants: list[VariantResult]
    best: VariantResult
    simulated: list[tuple[float, float | None, tuple[float, float] | None]]
    extremes: list[Extreme]
    embedded_texts: int
    embed_seconds: float
    match_seconds: float
    rows: list[dict[str, object]] = field(default_factory=list)


# Statistics


def auc(positives: Sequence[float], negatives: Sequence[float]) -> float | None:
    """The chance a random positive outranks a random negative (ties count half)."""
    if not positives or not negatives:
        return None
    pooled = sorted([(v, 1) for v in positives] + [(v, 0) for v in negatives])
    rank_sum = 0.0
    i = 0
    while i < len(pooled):
        j = i
        while j < len(pooled) and pooled[j][0] == pooled[i][0]:
            j += 1
        average_rank = (i + 1 + j) / 2
        rank_sum += average_rank * sum(label for _, label in pooled[i:j])
        i = j
    n_pos, n_neg = len(positives), len(negatives)
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def auc_ci(
    positives: Sequence[float], negatives: Sequence[float], *, rounds: int, rng: random.Random
) -> tuple[float, float] | None:
    """A bootstrap 95% interval, resampling each class with replacement."""
    if not positives or not negatives or rounds <= 0:
        return None
    values = sorted(
        auc(rng.choices(positives, k=len(positives)), rng.choices(negatives, k=len(negatives)))
        or 0.0
        for _ in range(rounds)
    )
    return values[int(0.025 * (rounds - 1))], values[math.ceil(0.975 * (rounds - 1))]


# Loading


def _rule_reason(row: PostingRow, rules: FilterRules) -> str | None:
    if rules.blocklist.blocks_company(row.company_name):
        return "company on the blocklist"
    title = check_title(row.title, rules)
    if not title.passed:
        return title.reason
    if row.locations:
        location = check_locations(row.locations, rules.locations)
        if not location.passed:
            return location.reason
    description = check_description(row.description_text, rules)
    return None if description.passed else description.reason


def load_labeled(session: Session, rules: FilterRules) -> list[Scored]:
    """Applied and Dismissed postings with a description, each in its group."""
    labeled = PostingRow.status.in_(("Applied", "Dismissed"))
    unmerged = PostingRow.merged_into_id.is_(None)
    shared = dict(
        session.execute(
            select(PostingRow.status_changed_at, func.count())
            .where(unmerged, PostingRow.status == "Dismissed")
            .where(PostingRow.status_changed_at.is_not(None))
            .group_by(PostingRow.status_changed_at)
        ).all()
    )
    scored: list[Scored] = []
    for row in session.scalars(select(PostingRow).where(labeled, unmerged).order_by(PostingRow.id)):
        description = (row.description_text or "").strip()
        if not description:
            continue
        reason = _rule_reason(row, rules)
        if row.status == "Applied":
            group, set_aside, note = "applied", None, reason
        elif reason is not None:
            group, set_aside, note = "rules", reason, None
        elif row.status_changed_at is None:
            group, set_aside, note = "pre-0005", "triaged before migration 0005", None
        elif shared.get(row.status_changed_at, 0) > 1:
            group, set_aside, note = "bulk", "bulk dismissal", None
        else:
            group, set_aside, note = "kept", None, None
        scored.append(
            Scored(
                row.id,
                row.status,
                group,
                set_aside,
                note,
                row.company_name,
                row.title,
                description,
                row.score_breakdown,
            )
        )
    return scored


def count_without_description(session: Session) -> Counter[str]:
    rows = session.execute(
        select(PostingRow.status, func.count())
        .where(PostingRow.merged_into_id.is_(None), PostingRow.status.in_(("Applied", "Dismissed")))
        .where(
            (PostingRow.description_text.is_(None)) | (func.trim(PostingRow.description_text) == "")
        )
        .group_by(PostingRow.status)
    )
    return Counter({status: count for status, count in rows})


def load_reference(session: Session, days: int = 7, limit: int = 800) -> list[Scored]:
    """Recent matches with a description (any status): what calibration anchors are measured
    on. "Recent" counts back from the newest posting, so a stale copy still works."""
    newest = session.scalar(select(func.max(PostingRow.first_seen_at)))
    if newest is None:
        return []
    rows = session.scalars(
        select(PostingRow)
        .where(
            PostingRow.tier == "instant",
            PostingRow.merged_into_id.is_(None),
            PostingRow.description_text.is_not(None),
            PostingRow.first_seen_at >= newest - timedelta(days=days),
        )
        .order_by(PostingRow.first_seen_at.desc())
        .limit(limit)
    )
    return [
        Scored(
            row.id,
            row.status,
            "reference",
            None,
            None,
            row.company_name,
            row.title,
            (row.description_text or "").strip(),
            row.score_breakdown,
        )
        for row in rows
        if (row.description_text or "").strip()
    ]


# The analysis


def _posting_text(posting: Scored, kind: str) -> str:
    return (
        f"{posting.title}\n{posting.description}" if kind == "title+desc" else posting.description
    )


class _VectorCache:
    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder
        self._vectors: dict[str, Vector] = {}
        self.seconds = 0.0

    def fill(self, texts: Iterable[str]) -> None:
        missing = list(dict.fromkeys(t for t in texts if t not in self._vectors))
        started = time.perf_counter()
        for start in range(0, len(missing), _EMBED_BATCH):
            batch = missing[start : start + _EMBED_BATCH]
            self._vectors.update(zip(batch, self._embedder.embed(batch), strict=True))
        self.seconds += time.perf_counter() - started

    def __getitem__(self, text: str) -> Vector:
        return self._vectors[text]

    def __len__(self) -> int:
        return len(self._vectors)


def _by_group(
    raws: Mapping[int, float],
    labeled: Sequence[Scored],
    key: Callable[[Scored], float] | None = None,
) -> tuple[list[float], dict[str, list[float]]]:
    value = key or (lambda posting: raws[posting.id])
    applied = [value(p) for p in labeled if p.group == "applied"]
    groups: dict[str, list[float]] = {name: [] for name in ("kept", *GROUPS)}
    for posting in labeled:
        if posting.group in groups:
            groups[posting.group].append(value(posting))
    return applied, groups


def run_eval(
    labeled: Sequence[Scored],
    reference: Sequence[Scored],
    *,
    resume_text: str,
    embedder: Embedder,
    bootstrap: int = 1000,
    seed: int = 0,
) -> EvalReport:
    rng = random.Random(seed)
    lines = resume_lines(resume_text)
    if not lines:
        raise ValueError("no content lines found in the resume")
    resume_sides: dict[str, list[str]] = {"lines": lines, "chunks": chunk_text("\n".join(lines))}
    postings = {p.id: p for p in [*reference, *labeled]}
    texts: dict[tuple[int, str, str], list[str]] = {}
    for posting in postings.values():
        for kind in ("title+desc", "desc"):
            text = _posting_text(posting, kind)
            texts[posting.id, kind, "block"] = [text]
            texts[posting.id, kind, "chunks"] = chunk_text(text)

    cache = _VectorCache(embedder)
    cache.fill([t for side in resume_sides.values() for t in side])
    cache.fill([t for chunks in texts.values() for t in chunks])

    results: list[VariantResult] = []
    raws_by_variant: dict[str, dict[int, RawMatch]] = {}
    started = time.perf_counter()
    for variant in VARIANTS:
        resume_vectors = [cache[t] for t in resume_sides[variant.resume]]
        matches: dict[int, RawMatch] = {}
        for posting_id in postings:
            chunk_vectors = [cache[t] for t in texts[posting_id, variant.text, variant.posting]]
            found = match(chunk_vectors, resume_vectors, variant.aggregation)
            if found is not None:
                matches[posting_id] = found
        raws_by_variant[variant.name] = matches
        raws = {pid: m.raw for pid, m in matches.items()}
        applied, groups = _by_group(raws, labeled)
        reference_raws = [raws[p.id] for p in reference if p.id in raws]
        results.append(
            VariantResult(
                variant,
                auc(applied, groups["kept"]),
                auc_ci(applied, groups["kept"], rounds=bootstrap, rng=rng),
                {name: auc(applied, groups[name]) for name in GROUPS},
                anchors(reference_raws or list(raws.values()) or [0.0]),
            )
        )
    match_seconds = time.perf_counter() - started
    best = max(results, key=lambda r: r.auc if r.auc is not None else -1.0)
    best_matches = raws_by_variant[best.variant.name]

    def baseline(name: str, key: Callable[[Scored], float]) -> Baseline:
        applied, groups = _by_group({}, labeled, key)
        return Baseline(
            name,
            auc(applied, groups["kept"]),
            auc_ci(applied, groups["kept"], rounds=bootstrap, rng=rng),
            {group: auc(applied, groups[group]) for group in GROUPS},
        )

    with_breakdown = [p for p in labeled if p.breakdown]
    baselines = [
        baseline("stack part", lambda p: p.part("stack")),
        baseline("title part", lambda p: p.part("title")),
        baseline("stored total", lambda p: p.part("total")),
        baseline("total without stack", lambda p: p.part("total") - p.part("stack")),
    ]

    simulated: list[tuple[float, float | None, tuple[float, float] | None]] = []
    for weight in SIMULATED_WEIGHTS:

        def total(p: Scored, weight: float = weight) -> float:
            resume = calibrate(best_matches[p.id].raw, best.anchors)
            return rescore_breakdown(p.breakdown or {}, resume=resume, weight=weight)[0]

        applied = [total(p) for p in with_breakdown if p.group == "applied"]
        kept = [total(p) for p in with_breakdown if p.group == "kept"]
        simulated.append(
            (weight, auc(applied, kept), auc_ci(applied, kept, rounds=bootstrap, rng=rng))
        )

    def line_of(found: RawMatch) -> str | None:
        if best.variant.resume != "lines":
            return None
        line = resume_sides["lines"][found.line_index]
        return line if len(line) <= _LINE_CHARS else line[: _LINE_CHARS - 1] + "…"

    extremes: list[Extreme] = []
    for group in ("applied", "kept"):
        members = sorted(
            (p for p in labeled if p.group == group and p.id in best_matches),
            key=lambda p: best_matches[p.id].raw,
            reverse=True,
        )
        picks = (
            members[:_EXTREMES] + members[-_EXTREMES:] if len(members) > 2 * _EXTREMES else members
        )
        extremes.extend(
            Extreme(group, p.title, p.company, best_matches[p.id].raw, line_of(best_matches[p.id]))
            for p in picks
        )

    rows: list[dict[str, object]] = []
    for posting in labeled:
        row: dict[str, object] = {
            "id": posting.id,
            "status": posting.status,
            "group": posting.group,
            "set_aside": posting.set_aside or "",
            "company": posting.company,
            "title": posting.title,
            "stack": posting.part("stack"),
            "title_part": posting.part("title"),
            "total": posting.part("total"),
        }
        for variant in VARIANTS:
            found = raws_by_variant[variant.name].get(posting.id)
            row[variant.name] = round(found.raw, 5) if found else ""
        rows.append(row)

    return EvalReport(
        counts=Counter(p.group for p in labeled),
        without_description=Counter(),
        applied_filtered_today=sum(1 for p in labeled if p.rules_note),
        reference_count=len(reference),
        baselines=baselines,
        variants=sorted(results, key=lambda r: -(r.auc or 0.0)),
        best=best,
        simulated=simulated,
        extremes=extremes,
        embedded_texts=len(cache),
        embed_seconds=cache.seconds,
        match_seconds=match_seconds,
        rows=rows,
    )


# Output


def _fmt(value: float | None) -> str:
    return "  –  " if value is None else f"{value:.3f}"


def _fmt_ci(ci: tuple[float, float] | None) -> str:
    return "" if ci is None else f"[{ci[0]:.3f}, {ci[1]:.3f}]"


def render(report: EvalReport) -> str:
    counts = report.counts
    out = [
        "# Resume similarity: Applied against Dismissed",
        "",
        "## Labels (postings with a description)",
        f"- applied: {counts['applied']} ({report.applied_filtered_today} of them filtered by "
        "today's rules)",
        f"- dismissed, kept: {counts['kept']}",
        "- dismissed, set aside: " + ", ".join(f"{name} {counts[name]}" for name in GROUPS),
        "- without a description (not scored): "
        + (", ".join(f"{k} {v}" for k, v in sorted(report.without_description.items())) or "none"),
        f"- recent matches for calibration: {report.reference_count}",
        "",
        "## AUC (applied against kept dismissals; 0.5 is chance)",
        "",
        f"{'signal':<40} {'AUC':>6} {'95% CI':>17}   " + "  ".join(f"vs {g:>8}" for g in GROUPS),
    ]
    for base in report.baselines:
        out.append(
            f"{base.name:<40} {_fmt(base.auc):>6} {_fmt_ci(base.ci):>17}   "
            + "  ".join(f"{_fmt(base.by_group[g]):>11}" for g in GROUPS)
        )
    out.append("")
    for result in report.variants:
        out.append(
            f"{result.variant.name:<40} {_fmt(result.auc):>6} {_fmt_ci(result.ci):>17}   "
            + "  ".join(f"{_fmt(result.by_group[g]):>11}" for g in GROUPS)
        )
    best = report.best
    out += [
        "",
        f"## Best variant: {best.variant.name}",
        f"- calibration anchors (p5, p95 of raw over recent matches): "
        f"{best.anchors[0]:.4f}, {best.anchors[1]:.4f}",
        "- simulated total with resume in place of stack:",
    ]
    for weight, value, ci in report.simulated:
        out.append(f"  - weight {weight:.2f}: AUC {_fmt(value)} {_fmt_ci(ci)}")
    out += [
        "",
        f"## Throughput: {report.embedded_texts} texts embedded in {report.embed_seconds:.1f} s "
        f"({report.embedded_texts / max(report.embed_seconds, 1e-9):.0f}/s); "
        f"{len(VARIANTS)} variants matched in {report.match_seconds:.1f} s",
        "",
    ]
    for group in ("applied", "kept"):
        members = [e for e in report.extremes if e.group == group]
        out.append(f"## Highest and lowest, {group} (best variant)")
        for extreme in members:
            line = f" ← {extreme.line}" if extreme.line else ""
            out.append(f"- {extreme.raw:.4f}  {extreme.title} · {extreme.company}{line}")
        out.append("")
    return "\n".join(out)


def write_csv(report: EvalReport, path: Path) -> None:
    if not report.rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(report.rows[0]))
        writer.writeheader()
        writer.writerows(report.rows)


def main(argv: Sequence[str] | None = None) -> int:
    from jobpulse.config import Profile, company_blocklist, filter_rules, load_legacy_config
    from jobpulse.db import profile as profile_db
    from jobpulse.db import repo
    from jobpulse.db.session import create_engine_for, make_session_factory
    from jobpulse.embeddings.fastembed import DEFAULT_CACHE_DIR, FastEmbedEmbedder
    from jobpulse.resume_files import read_resume_file

    parser = argparse.ArgumentParser(prog="resume_eval", description=__doc__.split("\n")[0])
    parser.add_argument("--database-url", required=True, help="a scratch copy, never the live DB")
    parser.add_argument("--resume", type=Path, required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="an old config.yaml with preferences (default: the profile stored in the database)",
    )
    parser.add_argument("--out", type=Path, required=True, help="directory for report and CSV")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--bootstrap", type=int, default=1000)
    args = parser.parse_args(argv)

    live = (Path("data") / "jobpulse.db").resolve()
    if (
        args.database_url.startswith("sqlite:///")
        and Path(args.database_url.removeprefix("sqlite:///")).resolve() == live
    ):
        parser.error("this is the live database; copy it to a scratch path first")

    resume = read_resume_file(args.resume)
    embedder = FastEmbedEmbedder(cache_dir=args.cache_dir)
    factory = make_session_factory(create_engine_for(args.database_url))
    with factory() as session:
        config: Profile | None = (
            load_legacy_config(args.config)
            if args.config is not None
            else profile_db.get(session).profile
        )
        if config is None:
            parser.error("no profile stored in that database; pass --config with an old config")
        rules = filter_rules(config)
        rules = FilterRules(
            **{
                **{name: getattr(rules, name) for name in rules.__dataclass_fields__},
                "blocklist": repo.blocklist(session, company_blocklist(config)),
            }
        )
        labeled = load_labeled(session, rules)
        reference = load_reference(session, days=args.days)
        missing = count_without_description(session)
    report = run_eval(
        labeled, reference, resume_text=resume.text, embedder=embedder, bootstrap=args.bootstrap
    )
    report.without_description = missing
    args.out.mkdir(parents=True, exist_ok=True)
    text = render(report)
    (args.out / "resume_eval.md").write_text(text, encoding="utf-8")
    write_csv(report, args.out / "resume_eval.csv")
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
