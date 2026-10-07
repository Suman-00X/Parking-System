"""Writes many rows in a few statements, for imports where one INSERT per row is the bottleneck.

Order: group rows by the columns they set and send each group in batches (bulk_insert);
server defaults (ids, timestamps) are left to the database, so nothing is read back.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import insert
from sqlalchemy.orm import Session

#: Rows per statement. Large enough to amortise the round trip, small enough to keep one batch's memory bounded.
BATCH_SIZE = 1000


def bulk_insert(session: Session, model: type[Any], rows: Iterable[Mapping[str, Any]], *, batch_size: int = BATCH_SIZE) -> int:
    """Insert `rows` into `model`'s table, `batch_size` at a time; returns how many were written."""
    table = model.__table__
    groups: dict[frozenset[str], list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault(frozenset(row), []).append(row)
    written = 0
    for group in groups.values():
        for start in range(0, len(group), batch_size):
            chunk = group[start : start + batch_size]
            session.execute(insert(table), list(chunk))
            written += len(chunk)
    return written
