"""Tiny DB-backed rate limiter (rate_events table) so limits hold across api restarts and replicas."""
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import RateEvent


async def hit(db: AsyncSession, kind: str, key: str) -> None:
    db.add(RateEvent(kind=kind, key=key[:320]))
    await db.commit()


async def count_since(db: AsyncSession, kind: str, key: str, seconds: int) -> int:
    since = datetime.now(UTC) - timedelta(seconds=seconds)
    return await db.scalar(
        sa.select(sa.func.count()).select_from(RateEvent)
        .where(RateEvent.kind == kind, RateEvent.key == key[:320], RateEvent.created_at > since)
    ) or 0


async def last_at(db: AsyncSession, kind: str, key: str) -> datetime | None:
    return await db.scalar(
        sa.select(sa.func.max(RateEvent.created_at)).where(RateEvent.kind == kind, RateEvent.key == key[:320])
    )


async def prune(db: AsyncSession, older_than_seconds: int = 86400) -> None:
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    await db.execute(sa.delete(RateEvent).where(RateEvent.created_at < cutoff))
    await db.commit()
