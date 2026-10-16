"""Paste a job link -> the company's whole board is added (for everyone) and its other roles are shown."""
import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.api.deps import CurrentUser, DbSession
from jobfinder.core.config import settings
from jobfinder.ingest.resolve import BadLink, clean_url, existing_board
from jobfinder.integrations.ats.custom import refusal_for_url, refusal_message
from jobfinder.integrations.directories import board_from_url
from jobfinder.models import Company, Job, JobLink, JobMatch, User
from jobfinder.scheduling import queue
from jobfinder.schemas.job_links import JobLinkIn, JobLinkOut, LinkDetail, LinkJob
from jobfinder.services import ratelimit

router = APIRouter(prefix="/api/job-links", tags=["job-links"])

RATE_KIND = "job_link"
MAX_JOBS = 200


def _enabled() -> None:
    if not settings.job_links_enabled:
        raise HTTPException(404, "Adding job links is turned off")


async def _out(db: AsyncSession, link: JobLink) -> JobLinkOut:
    company = await db.get(Company, link.company_id) if link.company_id else None
    count, pasted = 0, None
    if company:
        active = sa.and_(Job.company_id == company.id, Job.is_active.is_(True))
        count = await db.scalar(sa.select(sa.func.count()).select_from(Job).where(active)) or 0
        pasted = await db.scalar(sa.select(Job.id).where(active, Job.url == link.url).limit(1))
    pasted = pasted or link.job_id
    return JobLinkOut(
        id=link.id, url=link.url, status=link.status, company_id=link.company_id,
        company=company.name if company else None, ats=link.ats, rung=link.rung, error=link.error, job_count=count,
        pasted_job_id=pasted, created_at=link.created_at, resolved_at=link.resolved_at,
    )


async def _own(db: AsyncSession, user: User, link_id: int) -> JobLink:
    link = await db.get(JobLink, link_id)
    if link is None or link.user_id != user.id:
        raise HTTPException(404, "Link not found")
    return link


@router.post("", response_model=JobLinkOut, status_code=202)
async def add_link(body: JobLinkIn, user: CurrentUser, db: DbSession, response: Response):
    _enabled()
    try:
        url = clean_url(body.url)
    except BadLink as e:
        raise HTTPException(422, str(e)) from e

    dup = await db.scalar(sa.select(JobLink).where(JobLink.user_id == user.id, JobLink.url == url))
    if dup:
        response.status_code = 200
        return await _out(db, dup)

    limit = settings.job_links_per_user_day
    if await ratelimit.count_since(db, RATE_KIND, str(user.id), 86400) >= limit:
        raise HTTPException(429, f"You can add up to {limit} job links a day. Try again tomorrow.")
    await ratelimit.hit(db, RATE_KIND, str(user.id))

    refused = refusal_for_url(url)
    if refused is not None:  # terms/robots review said no: answer now, nothing is fetched
        link = JobLink(user_id=user.id, url=url, status="unresolved", rung="terms_refused",
                       error=refusal_message(refused)[:500], resolved_at=sa.func.now())
        db.add(link)
        await db.commit()
        await db.refresh(link)
        response.status_code = 201
        return await _out(db, link)

    link = JobLink(user_id=user.id, url=url, status="resolving")
    board = board_from_url(url)
    if board is not None:  # rung 1 is free: answer known boards straight away
        link.ats, link.slug = board
        known = await existing_board(db, *board)
        if known is not None:  # already polled on schedule: nothing to fetch
            link.status, link.company_id, link.rung = "resolved", known.id, "url_pattern"
            link.resolved_at = sa.func.now()
    db.add(link)
    await db.commit()
    await db.refresh(link)
    if link.status == "resolving":
        await queue.enqueue(db, "resolve_job_link", {"link_id": link.id}, user_id=user.id)
    else:
        response.status_code = 201
    return await _out(db, link)


@router.get("", response_model=list[JobLinkOut])
async def list_links(user: CurrentUser, db: DbSession):
    _enabled()
    links = (await db.scalars(
        sa.select(JobLink).where(JobLink.user_id == user.id).order_by(JobLink.created_at.desc(), JobLink.id.desc())
    )).all()
    return [await _out(db, link) for link in links]


@router.get("/{link_id}", response_model=LinkDetail)
async def get_link(link_id: int, user: CurrentUser, db: DbSession):
    _enabled()
    link = await _own(db, user, link_id)
    out = await _out(db, link)
    jobs: list[LinkJob] = []
    which = (Job.company_id == link.company_id) if link.company_id else (Job.id == link.job_id) if link.job_id else None
    if which is not None:
        rows = (await db.execute(
            sa.select(Job, JobMatch.id, JobMatch.llm_score)
            .outerjoin(JobMatch, sa.and_(JobMatch.job_id == Job.id, JobMatch.user_id == user.id))
            .where(which, Job.is_active.is_(True))
            .order_by(JobMatch.llm_score.desc().nulls_last(), Job.posted_at.desc().nulls_last(), Job.id.desc())
            .limit(MAX_JOBS)
        )).all()
        jobs = [
            LinkJob(id=j.id, title=j.title, location=j.location or "", url=j.url, posted_at=j.posted_at,
                    match_id=mid, score=score)
            for j, mid, score in rows
        ]
    return LinkDetail(**out.model_dump(), jobs=jobs)


@router.delete("/{link_id}", status_code=204)
async def delete_link(link_id: int, user: CurrentUser, db: DbSession):
    """Removes the user's link only. The board stays: other people may be getting matches from it."""
    _enabled()
    link = await _own(db, user, link_id)
    await db.delete(link)
    await db.commit()
