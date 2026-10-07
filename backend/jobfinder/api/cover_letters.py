from datetime import datetime
from typing import Annotated, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.auth.security import current_user
from jobfinder.db import get_db
from jobfinder.letters.docx_export import build_docx, filename
from jobfinder.models import CoverLetter, Job, JobMatch, Profile, Task, User
from jobfinder.scheduling import queue

router = APIRouter(prefix="/api/matches/{match_id}/cover-letter", tags=["cover letters"])

DB = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(current_user)]
Tone = Literal["professional", "warm", "concise", "enthusiastic"]
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_LETTER_CHARS = 10_000


class LetterOut(BaseModel):
    content: str
    tone: str
    edited: bool
    words: int
    stale: bool  # drafted before the latest profile update
    generated_at: datetime
    updated_at: datetime


class LetterState(BaseModel):
    letter: LetterOut | None
    pending_task_id: int | None  # a draft is being written right now


class GenerateIn(BaseModel):
    tone: Tone = "professional"
    notes: str = Field(default="", max_length=500)
    force: bool = False  # required to overwrite a letter the user has edited


class ContentIn(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_LETTER_CHARS)


class TaskRef(BaseModel):
    task_id: int


async def _match_and_job(db: AsyncSession, user: User, match_id: int) -> tuple[JobMatch, Job]:
    row = (
        await db.execute(
            sa.select(JobMatch, Job).join(Job, Job.id == JobMatch.job_id).where(JobMatch.id == match_id, JobMatch.user_id == user.id)
        )
    ).first()
    if not row:
        raise HTTPException(404, "Not found")
    return row


async def _letter(db: AsyncSession, user: User, job_id: int) -> CoverLetter | None:
    return (
        await db.execute(sa.select(CoverLetter).where(CoverLetter.user_id == user.id, CoverLetter.job_id == job_id))
    ).scalar_one_or_none()


def _out(letter: CoverLetter, profile_version: int) -> LetterOut:
    return LetterOut(
        content=letter.content, tone=letter.tone, edited=letter.edited, words=len(letter.content.split()),
        stale=letter.profile_version < profile_version, generated_at=letter.generated_at, updated_at=letter.updated_at,
    )


@router.get("", response_model=LetterState)
async def get_letter(match_id: int, user: CurrentUser, db: DB):
    _, job = await _match_and_job(db, user, match_id)
    letter = await _letter(db, user, job.id)
    profile = await db.get(Profile, user.id)
    pending = (
        await db.execute(
            sa.select(Task.id)
            .where(
                Task.kind == "cover_letter", Task.user_id == user.id, Task.status.in_(("queued", "running")),
                Task.payload["match_id"].as_integer() == match_id,
            )
            .order_by(Task.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return LetterState(letter=_out(letter, profile.version if profile else 1) if letter else None, pending_task_id=pending)


@router.post("", response_model=TaskRef, status_code=202)
async def generate(match_id: int, body: GenerateIn, user: CurrentUser, db: DB):
    """Draft (or redraft) the letter in the worker. Poll GET /api/tasks/{id}, then GET this resource."""
    _, job = await _match_and_job(db, user, match_id)
    profile = await db.get(Profile, user.id)
    if not profile or not profile.background:
        raise HTTPException(422, "Finish your profile first (onboarding)")
    existing = await _letter(db, user, job.id)
    if existing and existing.edited and not body.force:
        raise HTTPException(409, "You've edited this letter; redrafting would replace your changes.")
    task = await queue.enqueue(
        db, "cover_letter", {"match_id": match_id, "tone": body.tone, "notes": body.notes.strip()}, user_id=user.id, dedupe=True
    )
    return TaskRef(task_id=task.id)


@router.put("", response_model=LetterOut)
async def save_edits(match_id: int, body: ContentIn, user: CurrentUser, db: DB):
    _, job = await _match_and_job(db, user, match_id)
    letter = await _letter(db, user, job.id)
    if not letter:
        raise HTTPException(404, "No letter yet")
    new = body.content.replace("\r\n", "\n").strip()
    if new != letter.content:
        letter.content, letter.edited = new, True
        await db.commit()
        await db.refresh(letter)
    profile = await db.get(Profile, user.id)
    return _out(letter, profile.version if profile else 1)


@router.delete("", status_code=204)
async def delete_letter(match_id: int, user: CurrentUser, db: DB):
    _, job = await _match_and_job(db, user, match_id)
    await db.execute(sa.delete(CoverLetter).where(CoverLetter.user_id == user.id, CoverLetter.job_id == job.id))
    await db.commit()


@router.get("/download")
async def download(match_id: int, user: CurrentUser, db: DB):
    _, job = await _match_and_job(db, user, match_id)
    letter = await _letter(db, user, job.id)
    if not letter:
        raise HTTPException(404, "No letter yet")
    name = filename(job.company_name, job.title)
    return Response(
        build_docx(letter.content), media_type=DOCX,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name.replace(' ', '%20')}"},
    )
