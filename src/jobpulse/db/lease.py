"""The worker lease (M8): one row naming the worker allowed to poll this database, so a
second ``worker`` or ``up`` on it refuses to start and nothing is pushed twice. Like
``repo``, everything here leaves the transaction to the caller.

The holder renews the lease every minute; one not renewed by ``expires_at`` is free. A
holder on this host whose process is gone is free at once (a crash, or a container that
restarted with the same pid).
"""

import os
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from jobpulse.db.tables import WorkerLease

LEASE_ID = 1
TTL = timedelta(minutes=5)
RENEW_EVERY = timedelta(minutes=1)


class LeaseHeld(Exception):
    """Another worker holds the lease."""


@dataclass(frozen=True, slots=True)
class Holder:
    host: str
    pid: int
    nonce: str

    def __str__(self) -> str:
        return f"{self.host}:{self.pid}:{self.nonce}"

    @classmethod
    def here(cls) -> "Holder":
        return cls(socket.gethostname(), os.getpid(), uuid.uuid4().hex[:8])

    @classmethod
    def parse(cls, value: str) -> "Holder | None":
        host, _, rest = value.rpartition(":")
        host, _, pid = host.rpartition(":")
        if not host or not pid.isdigit():
            return None
        return cls(host, int(pid), rest)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # alive, but another user's
        return True
    return True


def _gone(holder: str, me: Holder, alive: Callable[[int], bool]) -> bool:
    """The holder is on this host and its process is gone (or is this one, restarted)."""
    other = Holder.parse(holder)
    if other is None or other.host != me.host:
        return False
    return other.pid == me.pid or not alive(other.pid)


def acquire(
    session: Session,
    me: Holder,
    *,
    now: datetime,
    ttl: timedelta = TTL,
    alive: Callable[[int], bool] = _pid_alive,
) -> None:
    """Take the lease, or raise ``LeaseHeld`` naming who has it."""
    row = session.get(WorkerLease, LEASE_ID, with_for_update=True)
    if row is None:
        session.add(WorkerLease(id=LEASE_ID, holder=str(me), acquired_at=now, expires_at=now + ttl))
        session.flush()
        return
    if row.holder != str(me) and row.expires_at > now and not _gone(row.holder, me, alive):
        raise LeaseHeld(
            f"another worker ({row.holder}) is polling this database (lease until "
            f"{row.expires_at.isoformat(timespec='seconds')}); stop it first, so nothing is "
            "pushed twice"
        )
    row.holder, row.acquired_at, row.expires_at = str(me), now, now + ttl
    session.flush()


def renew(session: Session, me: Holder, *, now: datetime, ttl: timedelta = TTL) -> bool:
    """Extend our lease; ``False`` when someone else holds it now."""
    row = session.get(WorkerLease, LEASE_ID, with_for_update=True)
    if row is None:
        session.add(WorkerLease(id=LEASE_ID, holder=str(me), acquired_at=now, expires_at=now + ttl))
    elif row.holder != str(me):
        return False
    else:
        row.expires_at = now + ttl
    session.flush()
    return True


def release(session: Session, me: Holder) -> None:
    row = session.get(WorkerLease, LEASE_ID)
    if row is not None and row.holder == str(me):
        session.delete(row)
        session.flush()
