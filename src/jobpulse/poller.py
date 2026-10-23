"""One poll cycle: fetch, diff, dedupe, prescreen, enrich, decide, notify.

The network phases run concurrently; every database phase is a synchronous block with no
``await`` inside. Live runs commit between phases, so no transaction is held across network
I/O. A dry run flushes instead and rolls the whole cycle back at the end.
"""

import asyncio
import dataclasses
import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy.orm import Session, sessionmaker

from jobpulse.config import (
    AppConfig,
    filter_rules,
    scoring_weights,
    short_labels,
)
from jobpulse.core.dedupe import posting_fingerprint
from jobpulse.core.detection import EvaluationContext, evaluate, plan_presence, prescreen
from jobpulse.core.models import (
    ATS_SOURCES,
    DEDUPE_SOURCES,
    JOB_BOARD_SOURCES,
    CompanyTarget,
    Decision,
    FetchResult,
    Posting,
    Precision,
    Target,
)
from jobpulse.core.scoring import priority_component
from jobpulse.core.timeparse import utcnow
from jobpulse.db import repo
from jobpulse.db.tables import PostingRow
from jobpulse.notify.base import Notifier, NotifierError
from jobpulse.notify.format import PushContent, instant_notification, overflow_notification
from jobpulse.sources.base import SourceAdapter

log = logging.getLogger(__name__)


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
    closed: int = 0
    reopened: int = 0
    filtered: int = 0
    stale: int = 0
    instant: list[str] = field(default_factory=list)
    digest: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    run_id: int | None = None

    def stats(self) -> dict[str, int]:
        return {
            "baseline_stored": self.baseline_stored,
            "merged": self.merged,
            "closed": self.closed,
            "reopened": self.reopened,
            "filtered": self.filtered,
            "stale": self.stale,
            "instant": len(self.instant),
            "digest": len(self.digest),
        }


@dataclass
class CycleReport:
    dry_run: bool
    targets: list[TargetReport] = field(default_factory=list)
    pushes: list[str] = field(default_factory=list)  # title lines sent (or, in a dry run, due)
    failed_pushes: int = 0
    held_back: int = 0


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


class Poller:
    def __init__(
        self,
        *,
        config: AppConfig,
        sessions: sessionmaker[Session],
        adapters: Mapping[str, SourceAdapter],
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._config = config
        self._sessions = sessions
        self._adapters = dict(adapters)
        self._notifier = notifier
        self._clock = clock
        self._rules = filter_rules(config)
        self._weights = scoring_weights(config)
        self._short_labels = short_labels(config)
        self._freshness = timedelta(hours=config.detection.freshness_hours)
        self._fingerprint_window = timedelta(days=config.detection.fingerprint_window_days)

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
            repo.sync_watchlist(session, self._config.watchlist, now=self._clock())
            targets = self._targets(session, sources, target_key)
            unit.checkpoint()

            started_at = self._clock()
            results = await asyncio.gather(*(self._fetch(target) for target in targets))
            survivors: list[_Survivor] = []
            for target, result in zip(targets, results, strict=True):
                target_report, target_survivors = self._ingest(session, target, result, started_at)
                report.targets.append(target_report)
                survivors.extend(target_survivors)
                unit.checkpoint()

            enriched = await asyncio.gather(*(self._enrich(survivor) for survivor in survivors))
            for survivor, posting in zip(survivors, enriched, strict=True):
                self._decide(session, survivor, posting)
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

            await self._notify(unit, report)
            return report
        finally:
            unit.close()

    # --- phases -----------------------------------------------------------------------

    def _targets(
        self, session: Session, sources: Collection[str] | None, target_key: str | None
    ) -> list[Target]:
        wanted = None if sources is None else set(sources)
        targets: list[Target] = []
        if wanted is None or wanted & ATS_SOURCES:
            ats = None if wanted is None else wanted & ATS_SOURCES
            targets.extend(repo.company_targets(session, ats=ats, token=target_key))
        if wanted is None or wanted & JOB_BOARD_SOURCES:
            sites = None if wanted is None else wanted & JOB_BOARD_SOURCES
            targets.extend(repo.search_targets(session, sites=sites, name=target_key))
        runnable = [target for target in targets if target.source in self._adapters]
        for target in targets:
            if target.source not in self._adapters:
                log.warning("no adapter for %s/%s; skipped", target.source, target.key)
        return runnable

    async def _fetch(self, target: Target) -> FetchResult:
        try:
            return await self._adapters[target.source].fetch(target)
        except Exception as exc:
            log.exception("%s/%s: adapter crashed", target.source, target.key)
            return FetchResult.failure(
                "other", f"adapter error: {type(exc).__name__}: {exc}", fetched_at=self._clock()
            )

    def _ingest(
        self, session: Session, target: Target, result: FetchResult, started_at: datetime
    ) -> tuple[TargetReport, list[_Survivor]]:
        """Phase A for one target: record the run, apply presence and closures, insert new
        postings, store a baseline silently, dedupe, and prescreen."""
        report = TargetReport(source=target.source, key=target.key)
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
        if result.not_modified:
            return report, []

        now = result.fetched_at
        company_id = target.company_id if isinstance(target, CompanyTarget) else None
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
            result, existing, close_after=self._config.detection.closure_missing_polls
        )
        repo.apply_presence(session, plan, now=now)
        report.closed = len(plan.closed_ids)
        report.reopened = len(plan.reopened_ids)

        baseline = not target.baseline_done
        inserted: list[tuple[int, Posting, str]] = []
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
        if baseline:
            repo.mark_baseline_done(session, target, now=now)
            report.is_baseline = True
            report.baseline_stored = len(inserted)
            return report, []
        report.new = len(inserted)

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
                    continue
            candidates.append(_Survivor(row_id, posting, now, target, report))

        token = target.token if isinstance(target, CompanyTarget) else None
        for row in repo.pending_rows(
            session,
            source=target.source,
            company_id=company_id,
            since=now - self._freshness,
            exclude_ids=[candidate.row_id for candidate in candidates],
        ):
            posting = repo.row_to_posting(row, company_token=token)
            candidates.append(_Survivor(row.id, posting, row.first_seen_at, target, report))

        survivors: list[_Survivor] = []
        for candidate in candidates:
            decision = prescreen(candidate.posting, self._context(candidate))
            if decision is None:
                survivors.append(candidate)
            else:
                repo.set_decision(session, candidate.row_id, decision)
                self._tally(candidate, candidate.posting, decision)
        return report, survivors

    async def _enrich(self, survivor: _Survivor) -> Posting:
        adapter = self._adapters.get(survivor.posting.source)
        if adapter is None:
            return survivor.posting
        try:
            return await adapter.enrich(survivor.posting)
        except Exception:
            log.exception(
                "enrichment of %s/%s failed", survivor.posting.source, survivor.posting.external_id
            )
            return survivor.posting

    def _decide(self, session: Session, survivor: _Survivor, posting: Posting) -> None:
        decision = evaluate(posting, self._context(survivor))
        repo.save_evaluation(session, survivor.row_id, posting, decision)
        self._tally(survivor, posting, decision)

    async def _notify(self, unit: _UnitOfWork, report: CycleReport) -> None:
        """Push every instant-tier posting not yet sent (so failed sends retry), best first,
        capped per cycle; the rest wait for the digest."""
        session = unit.session
        now = self._clock()
        due = repo.instant_unnotified(session, since=now - self._freshness)
        cap = self._config.notify.max_instant_per_cycle
        to_send, held = due[:cap], due[cap:]
        for row in held:
            repo.hold_back(session, row.id, reason="held back: over notify.max_instant_per_cycle")
        report.held_back = len(held)
        unit.checkpoint()

        if self._notifier is None and not report.dry_run:
            if to_send:
                log.warning(
                    "no notification channel configured; %d instant matches wait for the digest",
                    len(to_send),
                )
            return
        for row in to_send:
            message = instant_notification(
                self._push_content(row),
                now=now,
                priority=self._config.notify.ntfy.priority,
                tags=self._config.notify.ntfy.tags,
                short_labels=self._short_labels,
            )
            if report.dry_run or self._notifier is None:
                report.pushes.append(message.title)
                continue
            try:
                await self._notifier.send(message)
            except NotifierError as exc:
                report.failed_pushes += 1
                log.warning("push for posting %d failed (retrying next cycle): %s", row.id, exc)
                continue
            repo.mark_notified(session, row.id, now=self._clock())
            unit.checkpoint()
            report.pushes.append(message.title)
        if held and not report.dry_run and self._notifier is not None:
            try:
                await self._notifier.send(
                    overflow_notification(
                        len(held), dashboard_url=self._config.notify.dashboard_url
                    )
                )
            except NotifierError as exc:
                log.warning("overflow summary push failed: %s", exc)

    # --- helpers ------------------------------------------------------------------------

    def _context(self, survivor: _Survivor) -> EvaluationContext:
        target = survivor.target
        priority = (
            priority_component(target.priority, self._config.scoring.priority_cap)
            if isinstance(target, CompanyTarget)
            else 0.0
        )
        return EvaluationContext(
            now=self._clock(),
            first_seen_at=survivor.first_seen_at,
            freshness=self._freshness,
            rules=self._rules,
            weights=self._weights,
            instant_threshold=self._config.scoring.instant_threshold,
            company_priority=priority,
        )

    def _tally(self, survivor: _Survivor, posting: Posting, decision: Decision) -> None:
        report = survivor.report
        if decision.tier == "filtered":
            report.filtered += 1
        elif decision.tier == "stale":
            report.stale += 1
        elif decision.tier in ("instant", "digest"):
            content = dataclasses.replace(
                self._content_for(posting, survivor.first_seen_at),
                score=decision.score,
                breakdown=decision.breakdown,
            )
            line = instant_notification(
                content, now=self._clock(), short_labels=self._short_labels
            ).title
            score = f" (score {decision.score:.2f})" if decision.score is not None else ""
            (report.instant if decision.tier == "instant" else report.digest).append(line + score)

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
