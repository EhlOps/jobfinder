"""Tiny DB-backed rate limiter (rate_events table) so limits hold across api restarts and replicas.

Nothing here commits: the caller's boundary does (the request, a task, the worker loop). A hit that
must count even when the request then fails (a wrong password) goes through `core.db.side_session`.
"""
from datetime import UTC, datetime, timedelta

from jobfinder.core.db import AsyncSession
from jobfinder.repositories import rate_events

MAX_KEY = 320  # the column's width: a longer key (an email, a URL) is cut to it


async def hit(db: AsyncSession, kind: str, key: str) -> None:
    rate_events.add(db, kind, key[:MAX_KEY])
    await db.flush()


async def count_since(db: AsyncSession, kind: str, key: str, seconds: int) -> int:
    since = datetime.now(UTC) - timedelta(seconds=seconds)
    return await rate_events.count_since(db, kind, key[:MAX_KEY], since)


async def last_at(db: AsyncSession, kind: str, key: str) -> datetime | None:
    return await rate_events.last_at(db, kind, key[:MAX_KEY])


async def prune(db: AsyncSession, older_than_seconds: int = 86400) -> None:
    await rate_events.delete_before(db, datetime.now(UTC) - timedelta(seconds=older_than_seconds))
