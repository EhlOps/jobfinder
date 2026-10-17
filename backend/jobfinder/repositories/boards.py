"""Company boards for ingestion: which boards to fetch, board lookups, and the reads the coverage score
and audit make (engagement signals, coveted targets). Board health itself is set on the ORM rows by
services.ingestion; nothing here commits.
"""
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import Company, CovetedTarget, Job, JobLink, JobMatch, Profile, Task, User


async def existing(db: AsyncSession, ats: str, slug: str) -> Company | None:
    return await db.scalar(sa.select(Company).where(Company.ats == ats, Company.slug == slug))


async def enabled_boards(db: AsyncSession, *, ats: str | None = None, slug: str | None = None,
                         due_by: datetime | None = None) -> list[Company]:
    """Enabled boards by id, optionally of one ATS or slug, or only those due by `due_by`."""
    q = sa.select(Company).where(Company.enabled.is_(True)).order_by(Company.id)
    if ats:
        q = q.where(Company.ats == ats)
    if slug:
        q = q.where(Company.slug == slug)
    if due_by is not None:
        q = q.where(sa.or_(Company.next_fetch_at.is_(None), Company.next_fetch_at <= due_by))
    return list((await db.scalars(q)).all())


async def activated_statuses(db: AsyncSession) -> list[dict]:
    """The profile status of every activated user (what board selection targets)."""
    return list((await db.scalars(
        sa.select(Profile.status).join(User, User.id == Profile.user_id).where(User.activated_at.is_not(None))
    )).all())


# ── coverage: score signals ───────────────────────────────────────────────
async def recent_discovery_results(db: AsyncSession, limit: int = 10) -> list:
    return list((await db.scalars(
        sa.select(Task.result).where(Task.kind == "discover_boards", Task.status == "done")
        .order_by(Task.id.desc()).limit(limit)
    )).all())


async def pastes_since(db: AsyncSession, since: datetime) -> dict[int, int]:
    """company_id -> job links pointing at it created since `since`."""
    return dict((await db.execute(
        sa.select(JobLink.company_id, sa.func.count()).where(JobLink.company_id.is_not(None), JobLink.created_at >= since)
        .group_by(JobLink.company_id)
    )).all())


async def saves_since(db: AsyncSession, since: datetime) -> dict[int, int]:
    """company_id -> matches on its jobs saved or applied to, made since `since`."""
    return dict((await db.execute(
        sa.select(Job.company_id, sa.func.count()).join(JobMatch, JobMatch.job_id == Job.id)
        .where(Job.company_id.is_not(None), JobMatch.status.in_(("saved", "applied")), JobMatch.created_at >= since)
        .group_by(Job.company_id)
    )).all())


async def match_outcomes(db: AsyncSession) -> dict[int, tuple[int, float, int]]:
    """company_id -> (matches, mean llm_score, applied) over its jobs."""
    return {
        cid: (n, float(mean or 0), int(applied or 0))
        for cid, n, mean, applied in (await db.execute(
            sa.select(Job.company_id, sa.func.count(), sa.func.avg(JobMatch.llm_score),
                      sa.func.sum(sa.case((JobMatch.status == "applied", 1), else_=0)))
            .join(JobMatch, JobMatch.job_id == Job.id).where(Job.company_id.is_not(None))
            .group_by(Job.company_id)
        )).all()
    }


async def all_companies(db: AsyncSession) -> list[Company]:
    return list((await db.scalars(sa.select(Company))).all())


async def coveted_companies(db: AsyncSession, max_tier: int = 2) -> list[Company]:
    return list((await db.scalars(sa.select(Company).where(Company.coveted_tier <= max_tier))).all())


# ── coverage: targets ─────────────────────────────────────────────────────
async def linked_targets(db: AsyncSession) -> list[CovetedTarget]:
    return list((await db.scalars(sa.select(CovetedTarget).where(CovetedTarget.company_id.is_not(None)))).all())


async def target_for_company(db: AsyncSession, company_id: int) -> CovetedTarget | None:
    return await db.scalar(sa.select(CovetedTarget).where(CovetedTarget.company_id == company_id)
                           .order_by(CovetedTarget.id).limit(1))


async def target_by_domain(db: AsyncSession, domain: str) -> CovetedTarget | None:
    return await db.scalar(sa.select(CovetedTarget).where(CovetedTarget.domain == domain))


async def targets_to_audit(db: AsyncSession, gap_ids: list[int], limit: int) -> list[CovetedTarget]:
    """Targets with no board or a failing one (`gap_ids`), least recently tried first."""
    cond = CovetedTarget.company_id.is_(None)
    if gap_ids:
        cond = sa.or_(cond, CovetedTarget.company_id.in_(gap_ids))
    return list((await db.scalars(
        sa.select(CovetedTarget).where(cond)
        .order_by(CovetedTarget.last_resolved_at.asc().nulls_first(), CovetedTarget.id).limit(max(limit, 0))
    )).all())
