"""Pasted job links: the user's own rows, and the board facts their list shows, batched per page."""
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import Job, JobLink, JobMatch
from jobfinder.repositories import companies


@dataclass
class LinkFacts:
    """What a link's row shows about its board."""
    company: str | None = None
    job_count: int = 0
    pasted_job_id: int | None = None


async def get(db: AsyncSession, link_id: int) -> JobLink | None:
    return await db.get(JobLink, link_id)


async def by_url(db: AsyncSession, user_id: int, url: str) -> JobLink | None:
    return await db.scalar(sa.select(JobLink).where(JobLink.user_id == user_id, JobLink.url == url))


async def for_user(db: AsyncSession, user_id: int) -> list[JobLink]:
    return list((await db.scalars(
        sa.select(JobLink).where(JobLink.user_id == user_id).order_by(JobLink.created_at.desc(), JobLink.id.desc())
    )).all())


async def add(db: AsyncSession, link: JobLink) -> JobLink:
    """Insert and load the server defaults (id, created_at, resolved_at)."""
    db.add(link)
    await db.flush()
    await db.refresh(link)
    return link


async def delete(db: AsyncSession, link: JobLink) -> None:
    await db.delete(link)
    await db.flush()


async def facts(db: AsyncSession, links: list[JobLink]) -> dict[int, LinkFacts]:
    """Board name, active-job count and the pasted posting's job id for each link, in three queries
    whatever the number of links."""
    ids = {link.company_id for link in links if link.company_id}
    name = await companies.names(db, ids)
    count = await companies.active_job_counts(db, ids)
    pasted: dict[tuple[int, str], int] = {}
    urls = {link.url for link in links if link.company_id}
    if ids:
        pasted = {(c, u): j for c, u, j in (await db.execute(
            sa.select(Job.company_id, Job.url, sa.func.min(Job.id))
            .where(Job.is_active.is_(True), Job.company_id.in_(ids), Job.url.in_(urls))
            .group_by(Job.company_id, Job.url)
        )).all()}
    out = {}
    for link in links:
        f = LinkFacts()
        if link.company_id in name:  # a board that no longer exists shows as no board
            f.company, f.job_count = name[link.company_id], count.get(link.company_id, 0)
            f.pasted_job_id = pasted.get((link.company_id, link.url))
        f.pasted_job_id = f.pasted_job_id or link.job_id
        out[link.id] = f
    return out


async def jobs(db: AsyncSession, user_id: int, link: JobLink, limit: int) -> list[tuple[Job, int | None, float | None]]:
    """The link's board's active jobs (or just its one job), with this user's match id and score."""
    if link.company_id:
        which = Job.company_id == link.company_id
    elif link.job_id:
        which = Job.id == link.job_id
    else:
        return []
    rows = (await db.execute(
        sa.select(Job, JobMatch.id, JobMatch.llm_score)
        .outerjoin(JobMatch, sa.and_(JobMatch.job_id == Job.id, JobMatch.user_id == user_id))
        .where(which, Job.is_active.is_(True))
        .order_by(JobMatch.llm_score.desc().nulls_last(), Job.posted_at.desc().nulls_last(), Job.id.desc())
        .limit(limit)
    )).all()
    return [(j, mid, score) for j, mid, score in rows]
