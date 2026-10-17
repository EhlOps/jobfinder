"""A match's cover letter: read, queue a draft, save edits, delete, download as .docx."""
from fastapi import APIRouter

from jobfinder.api.deps import CurrentUser, DbSession
from jobfinder.schemas.common import TaskRef, file_download
from jobfinder.schemas.cover_letters import ContentIn, LetterGenerateIn, LetterOut, LetterState
from jobfinder.services import letters as service

router = APIRouter(prefix="/api/matches/{match_id}/cover-letter", tags=["cover letters"])


@router.get("", response_model=LetterState)
async def get_letter(match_id: int, user: CurrentUser, db: DbSession):
    return await service.state(db, user.id, match_id)


@router.post("", response_model=TaskRef, status_code=202)
async def generate(match_id: int, body: LetterGenerateIn, user: CurrentUser, db: DbSession):
    """Draft (or redraft) the letter in the worker. Poll GET /api/tasks/{id}, then GET this resource."""
    return await service.request_draft(db, user.id, match_id, body.tone, body.notes, body.force)


@router.put("", response_model=LetterOut)
async def save_edits(match_id: int, body: ContentIn, user: CurrentUser, db: DbSession):
    return await service.save_edits(db, user.id, match_id, body.content)


@router.delete("", status_code=204)
async def delete_letter(match_id: int, user: CurrentUser, db: DbSession):
    await service.delete(db, user.id, match_id)


@router.get("/download")
async def download(match_id: int, user: CurrentUser, db: DbSession):
    data, name = await service.docx(db, user.id, match_id)
    return file_download(data, name)
