"""The rate_events table behind services/ratelimit.py: one row per counted hit, pruned daily."""
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import RateEvent


def add(db: AsyncSession, kind: str, key: str) -> None:
    db.add(RateEvent(kind=kind, key=key))


async def count_since(db: AsyncSession, kind: str, key: str, since: datetime) -> int:
    return await db.scalar(
        sa.select(sa.func.count()).select_from(RateEvent)
        .where(RateEvent.kind == kind, RateEvent.key == key, RateEvent.created_at > since)
    ) or 0


async def last_at(db: AsyncSession, kind: str, key: str) -> datetime | None:
    return await db.scalar(
        sa.select(sa.func.max(RateEvent.created_at)).where(RateEvent.kind == kind, RateEvent.key == key)
    )


async def delete_before(db: AsyncSession, cutoff: datetime) -> None:
    await db.execute(sa.delete(RateEvent).where(RateEvent.created_at < cutoff))
