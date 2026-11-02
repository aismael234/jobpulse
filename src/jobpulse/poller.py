"""One poll cycle: read the profile, fetch, diff, dedupe, prescreen, enrich, decide (with
resume similarity in place of the stack keywords when a resume is stored, and the LLM layer's
second opinion near the threshold, when it's on), notify, check health, discover new boards,
and once a day in a watchlist cycle, sync the public seed (``seed.remote``).

Preferences come from the database profile, read at the start of every cycle: a change on
Settings applies from the next cycle, with no restart. Until a profile is onboarded, cycles
do nothing. When the profile's filters or scoring change, open matches are re-scored.

A posting whose detail couldn't be read (``EnrichmentDeferred``: LinkedIn's hourly page cap or
cool-down) stays pending, undecided, and the next cycle of its source tries again, until it
has waited ``linkedin.job_page_wait_hours``; then it's judged without the detail.

The network phases run concurrently; every database phase is a synchronous block with no
``await`` inside. Live runs commit between phases, so no transaction is held across network
I/O. A dry run flushes instead and rolls the whole cycle back at the end.
"""

import asyncio
import dataclasses
import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy.orm import Session, sessionmaker

from jobpulse.config import (
    AppConfig,
    InfraConfig,
    Profile,
    compose,
    filter_rules,
    scoring_hash,
    scoring_weights,
    short_labels,
)
from jobpulse.core.dedupe import posting_fingerprint
from jobpulse.core.detection import EvaluationContext, evaluate, plan_presence, prescreen
from jobpulse.core.filters import check_locations, check_title
from jobpulse.core.models import (
    ATS_SOURCES,
    DEDUPE_SOURCES,
    FEED_SOURCES,
    JOB_BOARD_SOURCES,
    CompanyTarget,
    Decision,
    FeedTarget,
    FetchResult,
    Posting,
    Precision,
    Target,
)
from jobpulse.core.palette import phone_bullet
from jobpulse.core.reposts import REPOST_AFTER, repost_reason
from jobpulse.core.scoring import DREAM_PRIORITY, priority_component
from jobpulse.core.similarity import ResumeMatch, Vector
from jobpulse.core.timeparse import utcnow
from jobpulse.db import profile as profile_db
from jobpulse.db import repo, saved_filters
from jobpulse.db import resume as resume_db
from jobpulse.db.profile import ProfileReader
from jobpulse.db.saved_filters import Saved
from jobpulse.db.tables import PostingRow
from jobpulse.discovery import Discoverer, Sighting
from jobpulse.embeddings.base import Embedder
from jobpulse.health import HealthMonitor, HealthRules
from jobpulse.llm.scorer import Ask, LlmScorer
from jobpulse.llm.source import LlmSource
from jobpulse.notify.base import Notification, Notifier, NotifierError
from jobpulse.notify.format import (
    PushContent,
    batch_notification,
    category_label,
    posting_notification,
    saved_filter_summary,
    title_line,
)
from jobpulse.rescoring import rescore_matches
from jobpulse.resume_scoring import (
    embed_resume,
    input_hash,
    needs_rescore,
    posting_text,
    rescore,
    resume_match,
)
from jobpulse.seed import SeedSync, SyncOutcome
from jobpulse.sources.base import EnrichmentDeferred, SourceAdapter
from jobpulse.sources.maintenance import Sleep

log = logging.getLogger(__name__)

Inserted = tuple[int, Posting, str]  # (row id, posting, fingerprint)
NOT_ONBOARDED = "no profile yet: answer the setup questions on the dashboard (/setup)"
LLM_CAP_WINDOW = timedelta(hours=24)  # llm.max_calls_per_day counts calls in this window
PUSH_CAP_WINDOW = timedelta(hours=24)  # notify.saved_filters.max_per_day counts this window


@dataclass
class TargetReport:
    source: str
    key: str
    ok: bool = True
    error_kind: str | None = None
    error: str | None = None
    fetched: int = 0
    new: int = 0
    is_baseline: bool = False
    baseline_stored: int = 0
    merged: int = 0
    adopted: int = 0
    closed: int = 0
    reopened: int = 0
    filtered: int = 0
    stale: int = 0
    waiting: int = 0  # left pending for a later cycle to read their detail
    instant: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    run_id: int | None = None
    not_modified: bool = False
    skipped: bool = False
    merges: list[str] = field(default_factory=list)

    def stats(self) -> dict[str, int]:
        return {
            "baseline_stored": self.baseline_stored,
            "merged": self.merged,
            "adopted": self.adopted,
            "closed": self.closed,
            "reopened": self.reopened,
            "filtered": self.filtered,
            "stale": self.stale,
            "waiting": self.waiting,
            "instant": len(self.instant),
        }


@dataclass
class CycleReport:
    dry_run: bool
    idle: str | None = None  # why nothing ran (no onboarded profile)
    profile_problem: str | None = None  # the stored profile is invalid: the last good one ran
    targets: list[TargetReport] = field(default_factory=list)
    pushes: list[str] = field(default_factory=list)  # title lines sent (or, in a dry run, due)
    push_summary: str | None = None  # the batch, or the saved-filter summary, they went in
    failed_pushes: int = 0  # matches whose push failed
    discovered: list[str] = field(default_factory=list)  # boards added (or that would be)
    discovery_deferred: int = 0
    discovery_skipped: list[str] = field(default_factory=list)  # valid, but over a limit
    discovery_capped: int = 0
    health: list[str] = field(default_factory=list)  # health messages sent (or due)
    pruned_runs: int = 0  # runs older than retention_days deleted this cycle
    retention_days: int | None = None
    llm_asked: int = 0  # answers the LLM gave this cycle
    llm_cached: int = 0  # answers taken from the per-posting cache
    llm_failed: int = 0  # calls that brought no answer
    llm_over_limit: int = 0  # band postings left to the rules: llm.max_calls_per_day reached
    llm_would_ask: int = 0  # dry run: band postings with no cached answer
    llm_stopped: str | None = None  # why the LLM layer stopped asking (a bad key)
    resume_matched: int = 0  # matches scored against the resume
    resume_embedded: int = 0  # descriptions embedded this cycle (the rest from the cache)
    resume_rescored: int = 0  # open matches re-scored at cycle start (the resume changed)
    resume_failed: str | None = None  # why the embedder couldn't be used: stack keywords instead
    rescored: int = 0  # open matches whose score changed with the profile's filters or scoring
    seed: SyncOutcome | None = None  # the public seed's sync, when one ran this cycle


@dataclass
class _Survivor:
    """A new posting that passed the cheap checks and goes on to enrichment."""

    row_id: int
    posting: Posting
    first_seen_at: datetime
    target: Target
    report: TargetReport


class _UnitOfWork:
    def __init__(self, session: Session, *, dry_run: bool) -> None:
        self.session = session
        self.dry_run = dry_run

    def checkpoint(self) -> None:
        if self.dry_run:
            self.session.flush()
        else:
            self.session.commit()

    def close(self) -> None:
        try:
            if self.dry_run:
                self.session.rollback()
        finally:
            self.session.close()


def _label(posting: Posting) -> str:
    return f"{posting.company_name} · {posting.title}" if posting.company_name else posting.title


class Poller:
    def __init__(
        self,
        *,
        infra: InfraConfig,
        sessions: sessionmaker[Session],
        adapters: Mapping[str, SourceAdapter],
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] = utcnow,
        discoverer: Discoverer | None = None,
        llm: LlmScorer | LlmSource | None = None,
        sleep: Sleep = asyncio.sleep,
        embedder: Embedder | None = None,
        on_profile: Callable[[AppConfig], None] | None = None,
        seed_sync: SeedSync | None = None,
    ) -> None:
        """``on_profile`` is called with the composed config whenever a new profile revision
        is loaded (the runtime updates the adapters' location aliases and the User-Agent)."""
        self._infra = infra
        self._sessions = sessions
        self._adapters = dict(adapters)
        self._notifier = notifier
        self._clock = clock
        self._discoverer = discoverer
        self._llm = llm
        self._sleep = sleep  # spaces saved-filter pushes out
        self._embedder = embedder
        self._resume_lock = asyncio.Lock()  # one re-score at a time across concurrent cycles
        self._on_profile = on_profile
        self._seed_sync = seed_sync
        self._health = (
            HealthMonitor(HealthRules.from_config(infra.health, infra.maintenance))
            if infra.health.enabled
            else None
        )
        self._freshness = timedelta(hours=infra.detection.freshness_hours)
        self._fingerprint_window = timedelta(days=infra.detection.fingerprint_window_days)
        self._detail_wait = timedelta(hours=infra.linkedin.job_page_wait_hours)
        self._next_prune: datetime | None = None  # run history is pruned at most once a day
        self._profiles = ProfileReader()
        self._revision: int | None = None  # the profile revision compiled below
        self._compile(compose(infra, Profile()))

    def _compile(self, config: AppConfig) -> None:
        self._config = config
        self._rules = filter_rules(config)
        self._config_blocklist = self._rules.blocklist
        self._blocklist = self._config_blocklist  # with the dashboard's blocks, per cycle
        self._weights = scoring_weights(config)
        self._short_labels = short_labels(config)

    async def run_cycle(
        self,
        *,
        sources: Collection[str] | None = None,
        target_key: str | None = None,
        dry_run: bool = False,
    ) -> CycleReport:
        report = CycleReport(dry_run=dry_run)
        unit = _UnitOfWork(self._sessions(), dry_run=dry_run)
        session = unit.session
        try:
            now = self._clock()
            if not self._load_profile(session, report):
                return report
            repo.sync_searches(session, self._config.searches, now=now)
            # Companies blocked from the dashboard count from the next cycle, without a restart.
            self._blocklist = repo.blocklist(
                session, self._config_blocklist, agencies=self._config.hide_agencies
            )
            self._rules = replace(self._rules, blocklist=self._blocklist)
            targets = self._targets(session, sources, target_key)
            unit.checkpoint()
            self._rescore_profile(unit, report)
            await self._rescore_resume(unit, report)

            started_at = self._clock()
            results = await asyncio.gather(*(self._fetch(target) for target in targets))
            survivors: list[_Survivor] = []
            sightings: list[Sighting] = []
            claimed: set[int] = set()  # rows already headed for a decision this cycle
            for target, result in zip(targets, results, strict=True):
                target_report, target_survivors = self._ingest(
                    session, target, result, started_at, claimed
                )
                report.targets.append(target_report)
                survivors.extend(target_survivors)
                sightings.extend(self._sightings(result))
                unit.checkpoint()

            details = await asyncio.gather(*(self._enrich(survivor) for survivor in survivors))
            survivors, enriched = self._hold_deferred(survivors, details)
            decisions = [
                evaluate(posting, self._context(survivor))
                for survivor, posting in zip(survivors, enriched, strict=True)
            ]
            decisions = await self._resume_matches(unit, report, survivors, enriched, decisions)
            decisions = await self._review(unit, report, survivors, enriched, decisions)
            for survivor, posting, decision in zip(survivors, enriched, decisions, strict=True):
                repo.save_evaluation(
                    session,
                    survivor.row_id,
                    posting,
                    decision,
                    repost_reason=self._repost_reason(session, survivor, posting),
                    decided_at=self._clock(),
                )
                self._tally(survivor, posting, decision)
            finished_at = self._clock()
            for target_report in report.targets:
                if target_report.run_id is not None:
                    repo.finish_run(
                        session,
                        target_report.run_id,
                        finished_at=finished_at,
                        new_count=target_report.new,
                        stats=target_report.stats(),
                    )
            unit.checkpoint()

            await self._notify(unit, report, sources)
            await self._check_health(unit, report)
            await self._discover(unit, report, sightings)
            await self._sync_seed(unit, report, sources, target_key)
            self._prune_runs(unit, report)
            return report
        finally:
            unit.close()

    # --- phases -----------------------------------------------------------------------

    def _load_profile(self, session: Session, report: CycleReport) -> bool:
        """Compile the stored profile when its revision moved; whether the cycle can run (a
        profile is onboarded). An invalid stored profile keeps the last good one."""
        stored = self._profiles.read(session)
        report.profile_problem = stored.problem
        if stored.problem is not None:
            log.warning("profile: %s", stored.problem)
        if stored.profile is None or not stored.onboarded:
            report.idle = stored.problem or NOT_ONBOARDED
            return False
        if stored.revision != self._revision:
            self._compile(compose(self._infra, stored.profile))
            self._revision = stored.revision
            if self._on_profile is not None:
                self._on_profile(self._config)
        return True

    def _rescore_profile(self, unit: _UnitOfWork, report: CycleReport) -> None:
        """Re-score open matches when the profile's filters or scoring differ from those they
        were last scored with. A profile seen for the first time (just imported or saved)
        is taken as what they were scored with."""
        session = unit.session
        digest = scoring_hash(self._config)
        stored = profile_db.scored_hash(session)
        if stored == digest:
            return
        if stored is not None:
            report.rescored = rescore_matches(
                session,
                rules=self._rules,
                weights=self._weights,
                freshness=self._freshness,
                priority_cap=self._config.scoring.priority_cap,
                applicant_cap=self._config.scoring.applicant_cap,
            )
            log.info("profile: re-scored %d open matches", report.rescored)
        profile_db.mark_scored(session, digest)
        unit.checkpoint()

    def _targets(
        self, session: Session, sources: Collection[str] | None, target_key: str | None
    ) -> list[Target]:
        """Companies first, then feeds, then searches, so that within one cycle the ATS row
        of a job exists before any copy of it is deduplicated."""
        wanted = None if sources is None else set(sources)
        targets: list[Target] = []
        if wanted is None or wanted & ATS_SOURCES:
            ats = None if wanted is None else wanted & ATS_SOURCES
            targets.extend(repo.company_targets(session, ats=ats, token=target_key))
        if (wanted is None or wanted & FEED_SOURCES) and self._infra.simplify.enabled:
            targets.extend(repo.feed_targets(session, self._infra.simplify, name=target_key))
        if wanted is None or wanted & JOB_BOARD_SOURCES:
            sites = None if wanted is None else wanted & JOB_BOARD_SOURCES
            targets.extend(repo.search_targets(session, sites=sites, name=target_key))
        runnable = [target for target in targets if target.source in self._adapters]
        for target in targets:
            if target.source not in self._adapters:
                log.warning("no adapter for %s/%s; skipped", target.source, target.key)
        return [target for target in runnable if not self._blocked(target)]

    def _blocked(self, target: Target) -> bool:
        """A company board stays unpolled while the blocklist names it or its company."""
        if not isinstance(target, CompanyTarget):
            return False
        return self._blocklist.blocks_board(target.ats, target.token) or (
            self._blocklist.blocks_company(target.name)
        )

    async def _fetch(self, target: Target) -> FetchResult:
        try:
            return await self._adapters[target.source].fetch(target)
        except Exception as exc:
            log.exception("%s/%s: adapter crashed", target.source, target.key)
            return FetchResult.failure(
                "other", f"adapter error: {type(exc).__name__}: {exc}", fetched_at=self._clock()
            )

    def _ingest(
        self,
        session: Session,
        target: Target,
        result: FetchResult,
        started_at: datetime,
        claimed: set[int],
    ) -> tuple[TargetReport, list[_Survivor]]:
        """Phase A for one target: record the run, apply presence and closures, insert new
        postings, link ATS rows to earlier copies, store a baseline silently, dedupe, and
        prescreen. ``claimed`` holds the rows earlier targets of this cycle took on; pending
        rows aren't tied to one search, so the first search of their source takes them."""
        report = TargetReport(source=target.source, key=target.key)
        if result.skipped:  # never attempted, so there is no run to record
            report.ok = False
            report.skipped = True
            report.error_kind = result.error_kind
            report.error = result.error
            log.info("%s/%s skipped: %s", target.source, target.key, result.error)
            return report, []
        report.run_id = repo.start_run(
            session, source=target.source, target=target.key, started_at=started_at, result=result
        )
        if not result.ok:
            report.ok = False
            report.error_kind = result.error_kind
            report.error = result.error
            log.warning(
                "%s/%s failed (%s): %s", target.source, target.key, result.error_kind, result.error
            )
            return report, []
        report.fetched = len(result.postings)
        report.warnings = list(result.warnings)
        report.not_modified = result.not_modified

        now = result.fetched_at
        company_id = target.company_id if isinstance(target, CompanyTarget) else None
        if isinstance(target, FeedTarget) and result.etag != target.etag:
            # Saved with this cycle's rows, so a crash can't skip a version of the feed.
            repo.save_feed_etag(session, target, etag=result.etag, now=now)
        if result.not_modified:
            # Nothing changed at the source; only rows an interrupted cycle left are open.
            if not target.baseline_done:
                return report, []
            pending = self._pending(session, target, company_id, report, now=now, exclude=claimed)
            claimed.update(candidate.row_id for candidate in pending)
            return report, self._prescreen(session, pending)

        inserted = self._store(session, target, result, company_id, report, now=now)
        baseline = not target.baseline_done
        if isinstance(target, CompanyTarget) and inserted:
            adoptions = repo.adopt_copies(
                session,
                inserted=inserted,
                since=now - self._fingerprint_window,
                now=now,
                baseline=baseline,
            )
            report.adopted = len(adoptions)
            for adoption in adoptions:
                log.info(
                    "%s/%s: posting %d took over %s posting %d (%s)",
                    target.source, target.key, adoption.primary_id, adoption.copy_source,
                    adoption.copy_id, adoption.how,
                )  # fmt: skip
        if baseline:
            repo.mark_baseline_done(session, target, now=now)
            report.is_baseline = True
            report.baseline_stored = len(inserted)
            return report, []
        report.new = len(inserted)

        candidates = self._dedupe(session, target, inserted, report, now=now)
        claimed.update(candidate.row_id for candidate in candidates)
        candidates += self._pending(session, target, company_id, report, now=now, exclude=claimed)
        claimed.update(candidate.row_id for candidate in candidates)
        return report, self._prescreen(session, candidates)

    def _store(
        self,
        session: Session,
        target: Target,
        result: FetchResult,
        company_id: int | None,
        report: TargetReport,
        *,
        now: datetime,
    ) -> list[Inserted]:
        """Presence and closures, then insert every posting not stored yet."""
        if isinstance(target, CompanyTarget):
            existing = repo.presence_for_company(
                session, source=target.source, company_id=target.company_id
            )
        else:
            existing = repo.presence_for_keys(
                session,
                source=target.source,
                external_ids=[posting.external_id for posting in result.postings],
            )
        plan = plan_presence(
            result, existing, close_after=self._infra.detection.closure_missing_polls
        )
        repo.apply_presence(session, plan, now=now)
        reported = (
            repo.close_reported(
                session, source=target.source, external_ids=result.closed_external_ids, now=now
            )
            if result.closed_external_ids
            else 0
        )
        report.closed = len(plan.closed_ids) + reported
        report.reopened = len(plan.reopened_ids)

        baseline = not target.baseline_done
        inserted: list[Inserted] = []
        for posting in result.postings:
            if posting.external_id in existing:
                continue
            fingerprint = posting_fingerprint(posting)
            row_id = repo.insert_if_absent(
                session,
                posting,
                company_id=company_id,
                fingerprint=fingerprint,
                tier="baseline" if baseline else "pending",
                tier_reason="silent baseline" if baseline else None,
                now=now,
            )
            if row_id is not None:
                inserted.append((row_id, posting, fingerprint))
        return inserted

    def _dedupe(
        self,
        session: Session,
        target: Target,
        inserted: list[Inserted],
        report: TargetReport,
        *,
        now: datetime,
    ) -> list[_Survivor]:
        """Rule 5: a new feed or job-board posting that copies a stored one is merged."""
        candidates: list[_Survivor] = []
        hosts: dict[str, str] | None = None
        for row_id, posting, fingerprint in inserted:
            if posting.source in DEDUPE_SOURCES:
                if hosts is None:
                    hosts = repo.greenhouse_hosts(session)
                match = repo.find_merge_target(
                    session,
                    posting,
                    fingerprint=fingerprint,
                    exclude_id=row_id,
                    since=now - self._fingerprint_window,
                    greenhouse_hosts=hosts,
                )
                if match is not None:
                    primary, how = match
                    repo.merge_into(
                        session,
                        primary=primary,
                        duplicate_id=row_id,
                        posting=posting,
                        how=how,
                        now=now,
                    )
                    report.merged += 1
                    report.merges.append(
                        f"{_label(posting)} ({posting.source} {posting.external_id}) "
                        f"into {primary.source} {primary.external_id} by {how}"
                    )
                    continue
            candidates.append(_Survivor(row_id, posting, now, target, report))
        return candidates

    def _pending(
        self,
        session: Session,
        target: Target,
        company_id: int | None,
        report: TargetReport,
        *,
        now: datetime,
        exclude: Collection[int],
    ) -> list[_Survivor]:
        """Rows an interrupted cycle inserted but never decided, and rows waiting for their
        detail."""
        token = target.token if isinstance(target, CompanyTarget) else None
        return [
            _Survivor(
                row.id,
                repo.row_to_posting(row, company_token=token),
                row.first_seen_at,
                target,
                report,
            )
            for row in repo.pending_rows(
                session,
                source=target.source,
                company_id=company_id,
                since=now - self._freshness,
                exclude_ids=exclude,
            )
        ]

    def _prescreen(self, session: Session, candidates: list[_Survivor]) -> list[_Survivor]:
        survivors: list[_Survivor] = []
        for candidate in candidates:
            decision = prescreen(candidate.posting, self._context(candidate))
            if decision is None:
                survivors.append(candidate)
            else:
                repo.set_decision(session, candidate.row_id, decision)
                self._tally(candidate, candidate.posting, decision)
        return survivors

    def _sightings(self, result: FetchResult) -> list[Sighting]:
        """Every URL on a feed or job-board posting, for watchlist discovery. A sighting is
        relevant when its posting passes the title and location filters."""
        if self._discoverer is None or not result.ok:
            return []
        sightings: list[Sighting] = []
        for posting in result.postings:
            if posting.source not in DEDUPE_SOURCES:
                continue
            if self._blocklist.blocks_company(posting.company_name):
                continue
            relevant = (
                check_title(posting.title, self._rules).passed
                and check_locations(posting.locations, self._rules.locations).passed
            )
            for url in dict.fromkeys(url for url in (posting.url, posting.apply_url) if url):
                sightings.append(
                    Sighting(
                        url=url,
                        company_name=posting.company_name,
                        source=posting.source,
                        relevant=relevant,
                    )
                )
        return sightings

    async def _enrich(self, survivor: _Survivor) -> tuple[Posting, str | None]:
        """The enriched posting, and why its detail couldn't be read now (else ``None``)."""
        adapter = self._adapters.get(survivor.posting.source)
        if adapter is None:
            return survivor.posting, None
        try:
            return await adapter.enrich(survivor.posting), None
        except EnrichmentDeferred as exc:
            return survivor.posting, str(exc)
        except Exception:
            log.exception(
                "enrichment of %s/%s failed", survivor.posting.source, survivor.posting.external_id
            )
            return survivor.posting, None

    def _hold_deferred(
        self, survivors: list[_Survivor], details: list[tuple[Posting, str | None]]
    ) -> tuple[list[_Survivor], list[Posting]]:
        """The survivors to decide now, with their postings. One whose detail couldn't be read
        stays pending for a later cycle while it has waited less than
        ``linkedin.job_page_wait_hours``; after that it's decided without the detail."""
        now = self._clock()
        kept: list[_Survivor] = []
        postings: list[Posting] = []
        for survivor, (posting, deferred) in zip(survivors, details, strict=True):
            if deferred is not None:
                waited = now - survivor.first_seen_at
                label = f"{posting.source}/{posting.external_id}"
                if waited < self._detail_wait:
                    survivor.report.waiting += 1
                    log.info("%s waits for a later cycle: %s", label, deferred)
                    continue
                log.info("%s judged without its detail after %s: %s", label, waited, deferred)
            kept.append(survivor)
            postings.append(posting)
        return kept, postings

    async def _rescore_resume(self, unit: _UnitOfWork, report: CycleReport) -> None:
        """At cycle start: embed the stored resume when its vectors are missing or from another
        model, then re-score open matches when it changed since the last re-score."""
        embedder = self._embedder
        if embedder is None:
            return
        async with self._resume_lock:
            session = unit.session
            stored = resume_db.get(session)
            if stored is None or not stored.lines:
                return
            if stored.embed_model != embedder.model:
                unit.checkpoint()  # nothing stays open while the model runs
                try:
                    vectors = await asyncio.to_thread(embed_resume, embedder, stored.lines)
                except Exception as exc:
                    report.resume_failed = f"couldn't embed the resume ({exc})"
                    log.warning("resume: %s", report.resume_failed)
                    return
                resume_db.save_vectors(session, model=embedder.model, vectors=vectors)
                stored = resume_db.get(session)
            if stored is None or not needs_rescore(session, stored):
                return
            result = rescore(session, rules=self._rules, weights=self._weights, now=self._clock())
            report.resume_rescored = result.rescored
            unit.checkpoint()

    async def _resume_matches(
        self,
        unit: _UnitOfWork,
        report: CycleReport,
        survivors: list[_Survivor],
        postings: list[Posting],
        decisions: list[Decision],
    ) -> list[Decision]:
        """Matches with a description, scored against the stored resume in place of the stack
        keywords. Descriptions already embedded with this model come from the cache; the rest
        are embedded in a thread and cached. Without a usable embedder or resume, the
        decisions stand."""
        embedder = self._embedder
        wanted = [
            index
            for index, (posting, decision) in enumerate(zip(postings, decisions, strict=True))
            if decision.tier == "instant" and posting_text(posting.description_text)
        ]
        if embedder is None or not wanted:
            return decisions
        session = unit.session
        stored = resume_db.get(session)
        if stored is None or stored.vectors is None or stored.embed_model != embedder.model:
            return decisions
        model = embedder.model
        texts = {index: posting_text(postings[index].description_text) for index in wanted}
        hashes = {index: input_hash(model, text) for index, text in texts.items()}
        cached = resume_db.posting_vectors(
            session, [survivors[index].row_id for index in wanted], model=model
        )
        found: dict[int, list[Vector]] = {}
        missing: list[int] = []
        for index in wanted:
            entry = cached.get(survivors[index].row_id)
            if entry is not None and entry[0] == hashes[index]:
                found[index] = entry[1]
            else:
                missing.append(index)
        unit.checkpoint()  # nothing stays open while the model runs
        if missing:
            unique = list(dict.fromkeys(texts[index] for index in missing))
            try:
                embedded = await asyncio.to_thread(embedder.embed, unique)
            except Exception as exc:
                report.resume_failed = f"couldn't embed {len(unique)} descriptions ({exc})"
                log.warning("resume: %s", report.resume_failed)
            else:
                by_text = dict(zip(unique, embedded, strict=True))
                report.resume_embedded += len(unique)
                fresh = {index: [by_text[texts[index]]] for index in missing}
                resume_db.save_posting_vectors(
                    session,
                    {
                        survivors[index].row_id: (hashes[index], vectors)
                        for index, vectors in fresh.items()
                    },
                    model=model,
                    now=self._clock(),
                )
                found.update(fresh)
        decided = list(decisions)
        for index, vectors in found.items():
            match: ResumeMatch | None = resume_match(vectors, stored)
            if match is None:
                continue
            context = replace(self._context(survivors[index]), resume=match)
            decided[index] = evaluate(postings[index], context)
            report.resume_matched += 1
        return decided

    async def _review(
        self,
        unit: _UnitOfWork,
        report: CycleReport,
        survivors: list[_Survivor],
        postings: list[Posting],
        decisions: list[Decision],
    ) -> list[Decision]:
        """The LLM layer's second opinion on the matches within ``llm.band`` of
        ``scoring.instant_threshold``: the answer cached for that exact question, else a call while
        ``llm.max_calls_per_day`` allows. A dry run never calls."""
        session = unit.session
        llm = self._llm
        if isinstance(llm, LlmSource):
            llm = await llm.current(session, self._config)
            report.llm_stopped = self._llm.problem if isinstance(self._llm, LlmSource) else None
        asks = llm.candidates(postings, decisions) if llm is not None else []
        if llm is None or not asks:
            return decisions
        cached = repo.cached_assessments(
            session, {survivors[ask.index].row_id: ask.request.input_hash for ask in asks}
        )
        used = repo.count_llm_calls(session, since=self._clock() - LLM_CAP_WINDOW)
        unit.checkpoint()  # nothing stays open while the LLM answers
        decided = list(decisions)
        fresh: list[Ask] = []
        for ask in asks:
            answer = cached.get(survivors[ask.index].row_id)
            if answer is None:
                fresh.append(ask)
            else:
                decided[ask.index] = llm.apply(decided[ask.index], answer)
                report.llm_cached += 1
        if not fresh:
            return decided
        if report.dry_run:
            report.llm_would_ask = len(fresh)
            return decided
        if not llm.can_ask:
            report.llm_stopped = llm.stopped or "no LLM client (no Anthropic API key on Settings)"
            return decided
        to_ask = fresh[: max(0, llm.max_calls_per_day - used)]
        report.llm_over_limit = len(fresh) - len(to_ask)
        replies = await llm.ask([ask.request for ask in to_ask])
        answered_at = self._clock()
        for ask, reply in zip(to_ask, replies, strict=True):
            row_id = survivors[ask.index].row_id
            if reply.asked:
                repo.record_assessment(
                    session,
                    posting_id=row_id,
                    input_hash=ask.request.input_hash,
                    reply=reply,
                    now=answered_at,
                )
            if reply.assessment is None:
                report.llm_failed += reply.asked
                log.warning("llm: no answer about posting %d: %s", row_id, reply.error)
                continue
            report.llm_asked += 1
            decided[ask.index] = llm.apply(decided[ask.index], reply.assessment)
        report.llm_stopped = llm.stopped
        return decided

    async def _notify(
        self, unit: _UnitOfWork, report: CycleReport, sources: Collection[str] | None
    ) -> None:
        """Push every match from this cycle's sources not yet sent (so a failed push is
        retried by the same job's next cycle), whatever its score, as one message. Once a
        saved filter exists, only the matches that fit one go out, one message each."""
        session = unit.session
        now = self._clock()
        due = repo.instant_unnotified(session, since=now - self._freshness, sources=sources)
        # Rows decided before their company joined the blocklist are dropped, not pushed.
        blocked = {row.id for row in due if self._blocklist.blocks_company(row.company_name)}
        for row_id in blocked:
            repo.set_decision(
                session, row_id, Decision(tier="filtered", reason="company is on the blocklist")
            )
        due = [row for row in due if row.id not in blocked]
        unit.checkpoint()

        if self._notifier is None and not report.dry_run:
            if due:
                log.warning("no notification channel configured; %d matches not pushed", len(due))
            return
        if not due:
            return
        filters = saved_filters.list_filters(session)
        if filters:
            await self._push_saved(unit, report, due, filters)
            return
        contents = [self._push_content(row) for row in due]
        message = batch_notification(
            contents,
            category=category_label(sources),
            priority=self._config.notify.ntfy.priority,
            tags=self._config.notify.ntfy.tags,
        )
        lines = [
            title_line(content, now=now, short_labels=self._short_labels) for content in contents
        ]
        if self._notifier is not None and not report.dry_run:
            try:
                await self._notifier.send(message)
            except NotifierError as exc:
                report.failed_pushes = len(due)
                log.warning("push of %d matches failed (retrying next cycle): %s", len(due), exc)
                return
            repo.mark_notified(session, [row.id for row in due], now=self._clock())
            unit.checkpoint()
        report.push_summary = message.body
        report.pushes = lines

    async def _push_saved(
        self,
        unit: _UnitOfWork,
        report: CycleReport,
        due: list[PostingRow],
        filters: list[Saved],
    ) -> None:
        """One message per match that fits an active saved filter decided after it started
        firing, a bullet per filter it fits. They go out fewest filters first (then lowest
        score), so the most-matched lands last, on top of the phone's list; spaced out and
        capped per cycle and per day, with one summary, sent first, for the rest. A match
        that fits none stays unpushed in New; a message that fails is retried next cycle."""
        session = unit.session
        now = self._clock()
        fits = saved_filters.fitting(
            session, filters, [row.id for row in due], now=now, decided_after_save=True
        )
        rows = sorted(
            (row for row in due if row.id in fits),
            key=lambda row: (len(fits[row.id]), row.score if row.score is not None else -1.0),
        )
        if not rows:
            return
        limits = self._config.notify.saved_filters
        ntfy = self._config.notify.ntfy
        color_blind = saved_filters.color_blind(session)
        left_today = limits.max_per_day - saved_filters.pushed_since(session, now - PUSH_CAP_WINDOW)
        split = len(rows) - max(0, min(limits.max_per_cycle, left_today, len(rows)))
        rest, singles = rows[:split], rows[split:]

        def bullet(saved: Saved) -> str:
            return phone_bullet(saved.row.slot, color_blind=color_blind)

        messages: list[tuple[Notification, list[PostingRow]]] = []
        if rest:
            counts = [
                (bullet(saved), sum(saved in fits[row.id] for row in rest))
                for saved in sorted(filters, key=lambda item: item.row.slot)
                if any(saved in fits[row.id] for row in rest)
            ]
            summary = saved_filter_summary(
                counts, total=len(rest), priority=ntfy.priority, tags=ntfy.tags
            )
            messages.append((summary, rest))
            report.push_summary = summary.body
        for row in singles:
            message = posting_notification(
                self._push_content(row),
                bullets="".join(bullet(saved) for saved in fits[row.id]),
                now=now,
                priority=ntfy.priority,
                tags=ntfy.tags,
                short_labels=self._short_labels,
            )
            messages.append((message, [row]))
            report.pushes.append(message.title)
        if self._notifier is None or report.dry_run:
            return
        for index, (message, covered) in enumerate(messages):
            if index:
                await self._sleep(limits.spacing_seconds)
            try:
                await self._notifier.send(message)
            except NotifierError as exc:
                report.failed_pushes = sum(len(rows) for _, rows in messages[index:])
                log.warning(
                    "saved-filter push failed; %d matches retried next cycle: %s",
                    report.failed_pushes, exc,
                )  # fmt: skip
                return
            sent_at = self._clock()
            repo.mark_notified(session, [row.id for row in covered], now=sent_at)
            if not (rest and index == 0):  # the summary doesn't count toward the day
                row = covered[0]
                saved_filters.record_pushes(
                    session, row.id, [saved.row.id for saved in fits[row.id]], now=sent_at
                )
            unit.checkpoint()

    async def _check_health(self, unit: _UnitOfWork, report: CycleReport) -> None:
        """Open and resolve incidents for the targets that just ran, then send what's due:
        one alert per incident and one recovery message, those of one source together. A
        source that landed on its maintenance page has one incident for all its targets. A
        failed send is retried next cycle."""
        if self._health is None:
            return
        ran = [target for target in report.targets if target.run_id]
        due = self._health.review(
            unit.session,
            {(target.source, target.key) for target in ran},
            now=self._clock(),
            maintenance={target.source for target in ran if target.error_kind == "maintenance"},
            answered={target.source for target in ran if target.ok},
        )
        unit.checkpoint()
        for message in due:
            title = message.notification.title
            if report.dry_run:
                report.health.append(title)
                continue
            if self._notifier is None:
                continue
            try:
                await self._notifier.send(message.notification)
            except NotifierError as exc:
                log.warning("health message %r failed (retrying next cycle): %s", title, exc)
                continue
            self._health.mark_sent(unit.session, message, now=self._clock())
            unit.checkpoint()
            report.health.append(title)

    async def _discover(
        self, unit: _UnitOfWork, report: CycleReport, sightings: list[Sighting]
    ) -> None:
        """Validate boards found in this cycle's feed and job-board URLs and add the good
        ones to the watchlist (``auto``, priority 0, baseline pending)."""
        if self._discoverer is None or not sightings:
            return
        session = unit.session
        # Blocked boards are as good as known: never validated, never added.
        known = repo.known_companies(session) | self._blocklist.boards
        hosts = repo.greenhouse_hosts(session)
        limit = self._infra.discovery.max_auto_companies
        room = (
            None
            if limit is None
            else max(0, limit - repo.count_polled_auto_companies(session, self._blocklist))
        )
        unit.checkpoint()  # nothing stays open while pages and validations run
        outcome = await self._discoverer.run(
            sightings, known=known, greenhouse_hosts=hosts.keys(), room=room
        )
        report.discovery_deferred = outcome.deferred
        report.discovery_capped = outcome.capped
        for found in outcome.too_big:
            ref = found.validation.ref
            name = found.candidate.company_name or found.validation.name or ref.token
            report.discovery_skipped.append(
                f"{ref.ats}/{ref.token} ({name}, {found.validation.job_count} jobs): more than "
                f"discovery.max_board_jobs ({self._infra.discovery.max_board_jobs})"
            )
        now = self._clock()
        for found in outcome.valid:
            ref = found.validation.ref
            name = found.candidate.company_name or found.validation.name or ref.token
            official = found.validation.name
            if official and self._blocklist.blocks_company(official):
                self._discoverer.reject(ref)
                log.info("discovery: %s/%s not added: %s is blocked", ref.ats, ref.token, official)
                continue
            if repo.add_discovered_company(
                session,
                ats=ref.ats,
                token=ref.token,
                region=ref.region,
                name=name,
                discovered_from=found.candidate.url,
                now=now,
            ):
                report.discovered.append(
                    f"{ref.ats}/{ref.token} ({name}, {found.validation.job_count} jobs) "
                    f"from {found.candidate.url}"
                )
                log.info(
                    "discovery: %s %s/%s from %s",
                    "would add" if report.dry_run else "added",
                    ref.ats, ref.token, found.candidate.url,
                )  # fmt: skip
        for found in outcome.invalid:
            log.info(
                "discovery: %s/%s not added: %s",
                found.candidate.ref.ats, found.candidate.ref.token, found.validation.error,
            )  # fmt: skip
        unit.checkpoint()

    async def _sync_seed(
        self,
        unit: _UnitOfWork,
        report: CycleReport,
        sources: Collection[str] | None,
        target_key: str | None,
    ) -> None:
        """Once a day, in a cycle of the watchlist boards (never a dry run or a single
        target): fetch the public seed and add its new boards, baseline pending."""
        sync = self._seed_sync
        if sync is None or report.dry_run or target_key is not None:
            return
        if sources is not None and not set(sources) & ATS_SOURCES:
            return
        if not sync.due(self._clock()):
            return
        report.seed = await sync.run(
            unit.session,
            unit.checkpoint,
            config=self._config,
            blocklist=self._blocklist,
            rules=self._rules,
        )

    def _prune_runs(self, unit: _UnitOfWork, report: CycleReport) -> None:
        """Delete runs older than ``retention.runs_days``, at most once a day, never in a
        dry run. The retention window always covers what the health rules read."""
        days = self._infra.retention.runs_days
        now = self._clock()
        if report.dry_run or days is None:
            return
        if self._next_prune is not None and now < self._next_prune:
            return
        report.pruned_runs = repo.delete_runs_before(unit.session, now - timedelta(days=days))
        report.retention_days = days
        unit.checkpoint()
        self._next_prune = now + timedelta(days=1)

    # --- helpers ------------------------------------------------------------------------

    @staticmethod
    def _repost_reason(session: Session, survivor: _Survivor, posting: Posting) -> str | None:
        """LinkedIn's marker, else the same requisition first seen a week or more before."""
        earlier = None
        if posting.reposted is None:
            earlier = repo.earlier_posting(
                session, survivor.row_id, posting, before=survivor.first_seen_at - REPOST_AFTER
            )
        return repost_reason(
            linkedin_marker=posting.reposted, earlier=earlier, first_seen_at=survivor.first_seen_at
        )

    def _context(self, survivor: _Survivor) -> EvaluationContext:
        target = survivor.target
        priority = (
            priority_component(target.priority, self._config.scoring.priority_cap)
            if isinstance(target, CompanyTarget)
            else 0.0
        )
        if self._rules.is_dream(survivor.posting.company_name):
            priority = DREAM_PRIORITY
        return EvaluationContext(
            now=self._clock(),
            first_seen_at=survivor.first_seen_at,
            freshness=self._freshness,
            rules=self._rules,
            weights=self._weights,
            company_priority=priority,
            applicant_cap=self._config.scoring.applicant_cap,
        )

    def _tally(self, survivor: _Survivor, posting: Posting, decision: Decision) -> None:
        report = survivor.report
        if decision.tier == "filtered":
            report.filtered += 1
        elif decision.tier == "stale":
            report.stale += 1
        elif decision.tier == "instant":
            content = dataclasses.replace(
                self._content_for(posting, survivor.first_seen_at),
                score=decision.score,
                breakdown=decision.breakdown,
            )
            line = title_line(content, now=self._clock(), short_labels=self._short_labels)
            score = f" (score {decision.score:.2f})" if decision.score is not None else ""
            report.instant.append(line + score)

    @staticmethod
    def _content_for(posting: Posting, first_seen_at: datetime) -> PushContent:
        return PushContent(
            company=posting.company_name,
            title=posting.title,
            locations=posting.locations,
            posted_at=posting.posted_at,
            precision=posting.posted_at_precision,
            first_seen_at=first_seen_at,
            source=posting.source,
            click_url=posting.apply_url or posting.url,
        )

    @staticmethod
    def _push_content(row: PostingRow) -> PushContent:
        return PushContent(
            company=row.company_name,
            title=row.title,
            locations=tuple(row.locations or ()),
            posted_at=row.posted_at,
            precision=cast(Precision, row.posted_at_precision),
            first_seen_at=row.first_seen_at,
            source=row.source,
            click_url=row.apply_url or row.url,
            score=row.score,
            breakdown=row.score_breakdown,
        )
