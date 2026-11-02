"""Secrets saved on Settings (M6). ``credentials.SecretStore`` decides which value wins;
this only reads and writes the rows. The transaction is the caller's."""

from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.orm import Session

from jobpulse.db.tables import SecretRow


def get_all(session: Session) -> dict[str, str]:
    return {row.name: row.value for row in session.scalars(select(SecretRow))}


def put(session: Session, name: str, value: str, *, now: datetime) -> None:
    row = session.get(SecretRow, name)
    if row is None:
        session.add(SecretRow(name=name, value=value, updated_at=now))
    else:
        row.value = value
        row.updated_at = now
    session.flush()


def clear(session: Session, name: str) -> bool:
    result = cast(
        CursorResult[Any], session.execute(delete(SecretRow).where(SecretRow.name == name))
    )
    return result.rowcount > 0
