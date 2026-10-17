"""Paste a job link -> the company's whole board is added (for everyone) and its other roles are shown."""
from fastapi import APIRouter, Response

from jobfinder.api.deps import CurrentUser, DbSession
from jobfinder.schemas.job_links import JobLinkIn, JobLinkOut, LinkDetail
from jobfinder.services import job_links

router = APIRouter(prefix="/api/job-links", tags=["job-links"])


@router.post("", response_model=JobLinkOut, status_code=202)
async def add_link(body: JobLinkIn, user: CurrentUser, db: DbSession, response: Response):
    out, response.status_code = await job_links.add(db, user, body.url)
    return out


@router.get("", response_model=list[JobLinkOut])
async def list_links(user: CurrentUser, db: DbSession):
    return await job_links.list_links(db, user)


@router.get("/{link_id}", response_model=LinkDetail)
async def get_link(link_id: int, user: CurrentUser, db: DbSession):
    return await job_links.detail(db, user, link_id)


@router.delete("/{link_id}", status_code=204)
async def delete_link(link_id: int, user: CurrentUser, db: DbSession):
    """Removes the user's link only. The board stays: other people may be getting matches from it."""
    await job_links.delete(db, user, link_id)
