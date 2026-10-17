"""A match's tailored resume: read, queue tailoring, save edits, delete, download as .docx or .pdf."""
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from jobfinder.api.deps import CurrentUser, DbSession, Store
from jobfinder.schemas.common import TaskRef, file_download
from jobfinder.schemas.resumes import EditIn, ResumeGenerateIn, ResumeOut, ResumeState
from jobfinder.services import resumes as service

router = APIRouter(prefix="/api/matches/{match_id}/resume", tags=["resumes"])


@router.get("", response_model=ResumeState)
async def get_resume(match_id: int, user: CurrentUser, db: DbSession, store: Store):
    return await service.state(db, store, user.id, match_id)


@router.post("", response_model=TaskRef, status_code=202)
async def generate(match_id: int, body: ResumeGenerateIn, user: CurrentUser, db: DbSession, store: Store):
    """Tailor (or re-tailor) the resume in the worker. Poll GET /api/tasks/{id}, then GET this resource."""
    return await service.request_tailor(db, store, user.id, match_id, body.force)


@router.put("", response_model=ResumeOut)
async def save_edits(match_id: int, body: EditIn, user: CurrentUser, db: DbSession, store: Store):
    return await service.save_edits(db, store, user.id, match_id, body.content)


@router.delete("", status_code=204)
async def delete_resume(match_id: int, user: CurrentUser, db: DbSession, store: Store):
    await service.delete(db, store, user.id, match_id)


@router.get("/download")
async def download(match_id: int, user: CurrentUser, db: DbSession, store: Store, format: Annotated[Literal["docx", "pdf"], Query()] = "docx"):
    data, name, media = await service.export(db, store, user.id, match_id, format)
    return file_download(data, name, media)
