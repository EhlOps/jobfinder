"""Coveted targets: employers we want covered, with or without a board yet."""
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import CovetedTarget


async def get(db: AsyncSession, target_id: int) -> CovetedTarget | None:
    return await db.get(CovetedTarget, target_id)


async def all_targets(db: AsyncSession) -> list[CovetedTarget]:
    return list((await db.scalars(sa.select(CovetedTarget).order_by(CovetedTarget.id))).all())


async def by_domain(db: AsyncSession, domain: str) -> CovetedTarget | None:
    return await db.scalar(sa.select(CovetedTarget).where(CovetedTarget.domain == domain))


async def add(db: AsyncSession, target: CovetedTarget) -> CovetedTarget:
    db.add(target)
    await db.flush()
    return target


async def delete(db: AsyncSession, target: CovetedTarget) -> None:
    await db.delete(target)
    await db.flush()
