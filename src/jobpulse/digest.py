"""The digest: twice a day, one message listing the matches that didn't get a push of their
own, with a link to the dashboard.

It covers digest-tier postings, held-back instant matches, and instant matches whose push
window passed without a successful send. Closed, merged, already triaged (status other than
New) and blocked-company postings are left out. Each posting is claimed (``digest_sent_at``)
before the message goes out and released if the send fails, so it appears in at most one
digest.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy.orm import Session, sessionmaker

from jobpulse.config import AppConfig, company_blocklist, short_labels
from jobpulse.core.models import Precision
from jobpulse.core.timeparse import utcnow
from jobpulse.db import repo
from jobpulse.db.tables import PostingRow
from jobpulse.notify.base import Notification, Notifier, NotifierError
from jobpulse.notify.format import PushContent, digest_notification

log = logging.getLogger(__name__)

_ANY_ADDRESS = ("0.0.0.0", "::", "")


@dataclass(frozen=True, slots=True)
class DigestResult:
    count: int  # postings the message covers
    notification: Notification | None = None
    sent: bool = False
    error: str | None = None


def dashboard_link(config: AppConfig) -> str:
    """``notify.dashboard_url``, else the dashboard's local address."""
    if config.notify.dashboard_url:
        return config.notify.dashboard_url
    host = config.dashboard.host
    if host in _ANY_ADDRESS:
        host = "127.0.0.1"
    elif ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{config.dashboard.port}/"


def _content(row: PostingRow) -> PushContent:
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
    )


class Digest:
    def __init__(
        self,
        *,
        config: AppConfig,
        sessions: sessionmaker[Session],
        notifier: Notifier | None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._config = config
        self._sessions = sessions
        self._notifier = notifier
        self._clock = clock
        self._blocklist = company_blocklist(config)
        self._short_labels = short_labels(config)
        self._freshness = timedelta(hours=config.detection.freshness_hours)

    async def run(self, *, dry_run: bool = False) -> DigestResult:
        """Build the digest and, unless it's a dry run, send it. Nothing is sent when no
        posting is due."""
        now = self._clock()
        with self._sessions() as session:
            rows = [
                row
                for row in repo.digest_candidates(session, push_window_start=now - self._freshness)
                if not self._blocklist.blocks_company(row.company_name)
            ]
            if not rows:
                return DigestResult(count=0)
            message = digest_notification(
                [_content(row) for row in rows],
                now=now,
                dashboard_url=dashboard_link(self._config),
                short_labels=self._short_labels,
            )
            if dry_run:
                return DigestResult(count=len(rows), notification=message)
            if self._notifier is None:
                log.warning(
                    "digest: no notification channel configured; %d postings wait", len(rows)
                )
                return DigestResult(
                    count=len(rows), notification=message, error="no notification channel"
                )
            repo.claim_for_digest(session, [row.id for row in rows], now=now)
            session.commit()  # claimed before the send: no other digest can take them

        try:
            await self._notifier.send(message)
        except NotifierError as exc:
            with self._sessions() as session:
                repo.release_digest_claim(session, claimed_at=now)
                session.commit()
            log.warning(
                "digest of %d postings failed (retried at the next digest): %s", len(rows), exc
            )
            return DigestResult(count=len(rows), notification=message, error=str(exc))
        log.info("digest sent: %d postings", len(rows))
        return DigestResult(count=len(rows), notification=message, sent=True)
