"""Boards (the companies table): lookups and active-job counts, batched so lists cost one query each."""
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import Company, Job


async def get(db: AsyncSession, company_id: int) -> Company | None:
    return await db.get(Company, company_id)


async def names(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    return dict((await db.execute(sa.select(Company.id, Company.name).where(Company.id.in_(ids)))).all())


async def active_job_counts(db: AsyncSession, ids: set[int] | None = None) -> dict[int, int]:
    """Active jobs per company; all companies when `ids` is None. Companies with none are absent."""
    q = sa.select(Job.company_id, sa.func.count()).where(Job.is_active.is_(True)).group_by(Job.company_id)
    if ids is not None:
        if not ids:
            return {}
        q = q.where(Job.company_id.in_(ids))
    return dict((await db.execute(q)).all())


async def for_sources(db: AsyncSession) -> list[Company]:
    """Every board, disabled first, then the most failures."""
    return list((await db.scalars(
        sa.select(Company).order_by(Company.enabled, Company.consecutive_failures.desc(), Company.name, Company.id)
    )).all())


async def coveted(db: AsyncSession) -> list[Company]:
    """Tier 1-2 boards, plus any an admin pinned or excluded."""
    return list((await db.scalars(
        sa.select(Company).where(sa.or_(Company.coveted_tier <= 2, Company.coveted_pinned.is_not(None)))
    )).all())


async def cadence_by_tier(db: AsyncSession, now: datetime) -> list[tuple[int | None, int, int]]:
    """(tier, enabled boards, of those due now) per coveted tier."""
    due = sa.func.count().filter((Company.next_fetch_at.is_(None)) | (Company.next_fetch_at <= now))
    rows = (await db.execute(
        sa.select(Company.coveted_tier, sa.func.count(), due).where(Company.enabled.is_(True))
        .group_by(Company.coveted_tier).order_by(Company.coveted_tier.nulls_last())
    )).all()
    return [(t, n, d) for t, n, d in rows]
