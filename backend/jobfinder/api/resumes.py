import json
import re
from datetime import datetime
from typing import Annotated, Literal

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.api.deps import CurrentUser, DbSession
from jobfinder.models import Job, JobMatch, Profile, Task, User
from jobfinder.resumes import keywords, service
from jobfinder.resumes.export import render_docx, render_pdf, resume_to_text
from jobfinder.scheduling import queue
from jobfinder.schemas.common import TaskRef
from jobfinder.schemas.resumes import (
    CoverageOut,
    EditIn,
    ResumeGenerateIn,
    ResumeOut,
    ResumeState,
    TailoredResumeOut,
)
from jobfinder.storage.documents import DocumentMeta, DocumentStore, NotFound, get_store

router = APIRouter(prefix="/api/matches/{match_id}/resume", tags=["resumes"])

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF = "application/pdf"
EDITED_TAG = service.EDITED_TAG


async def _job(db: AsyncSession, user: User, match_id: int) -> Job:
    job = (
        await db.execute(
            sa.select(Job).join(JobMatch, Job.id == JobMatch.job_id).where(JobMatch.id == match_id, JobMatch.user_id == user.id)
        )
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Not found")
    return job


def _current(store: DocumentStore, user_id: int, match_id: int) -> tuple[DocumentMeta, TailoredResumeOut] | None:
    metas = store.list(user_id, kind="resume", tags=service.match_tags(match_id))
    if not metas:
        return None
    meta = max(metas, key=lambda m: m.created_at)
    try:  # the file holds ai.schemas.TailoredResume JSON, which has the same fields (test_api_architecture checks)
        return meta, TailoredResumeOut.model_validate(json.loads(store.read_text(user_id, meta.id)))
    except (ValueError, ValidationError, NotFound):
        return None  # unreadable file: treat as no resume so the user can regenerate


def _out(job: Job, meta: DocumentMeta, resume: TailoredResumeOut) -> ResumeOut:
    terms = keywords.extract_terms(f"{job.title}\n{job.description_text}")
    cov = keywords.coverage(resume_to_text(resume), terms, job.source)  # the ATS comes from the job's source
    return ResumeOut(
        content=resume, edited=meta.tags.get(EDITED_TAG) == "1",
        coverage=CoverageOut(percent=cov.percent, covered=cov.covered, missing=cov.missing, loose=cov.loose, ats=cov.ats, notes=cov.notes),
        generated_at=datetime.fromisoformat(meta.created_at), updated_at=datetime.fromisoformat(meta.fetched_at or meta.created_at),
    )


@router.get("", response_model=ResumeState)
async def get_resume(match_id: int, user: CurrentUser, db: DbSession):
    job = await _job(db, user, match_id)
    found = _current(get_store(), user.id, match_id)
    pending = (
        await db.execute(
            sa.select(Task.id)
            .where(
                Task.kind == "resume_tailor", Task.user_id == user.id, Task.status.in_(("queued", "running")),
                Task.payload["match_id"].as_integer() == match_id,
            )
            .order_by(Task.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return ResumeState(resume=_out(job, *found) if found else None, pending_task_id=pending)


@router.post("", response_model=TaskRef, status_code=202)
async def generate(match_id: int, body: ResumeGenerateIn, user: CurrentUser, db: DbSession):
    """Tailor (or re-tailor) the resume in the worker. Poll GET /api/tasks/{id}, then GET this resource."""
    await _job(db, user, match_id)
    profile = await db.get(Profile, user.id)
    if not profile or not profile.background:
        raise HTTPException(422, "Finish your profile first (onboarding)")
    found = _current(get_store(), user.id, match_id)
    if found and found[0].tags.get(EDITED_TAG) == "1" and not body.force:
        raise HTTPException(409, "You've edited this resume; regenerating would replace your changes.")
    payload = {"match_id": match_id, "force": True} if body.force else {"match_id": match_id}
    task = await queue.enqueue(db, "resume_tailor", payload, user_id=user.id, dedupe=True)
    return TaskRef(task_id=task.id)


@router.put("", response_model=ResumeOut)
async def save_edits(match_id: int, body: EditIn, user: CurrentUser, db: DbSession):
    job = await _job(db, user, match_id)
    store = get_store()
    found = _current(store, user.id, match_id)
    if not found:
        raise HTTPException(404, "No resume yet")
    meta, old = found
    if body.content != old:
        text = json.dumps(body.content.model_dump(), indent=1)
        try:
            meta = store.replace_text(user.id, meta.id, text, fetched=True)
            meta = store.set_tags(user.id, meta.id, {**service.match_tags(match_id), EDITED_TAG: "1"})
        except NotFound:  # a regeneration replaced the document while we were saving
            raise HTTPException(409, "The resume was just regenerated; reload and edit again.") from None
    return _out(job, meta, body.content)


@router.delete("", status_code=204)
async def delete_resume(match_id: int, user: CurrentUser, db: DbSession):
    await _job(db, user, match_id)
    store = get_store()
    for meta in store.list(user.id, kind="resume", tags=service.match_tags(match_id)):
        try:
            store.delete(user.id, meta.id)
        except NotFound:
            pass  # already gone


@router.get("/download")
async def download(match_id: int, user: CurrentUser, db: DbSession, format: Annotated[Literal["docx", "pdf"], Query()] = "docx"):
    job = await _job(db, user, match_id)
    found = _current(get_store(), user.id, match_id)
    if not found:
        raise HTTPException(404, "No resume yet")
    base = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 ._-]+", "", f"Resume - {job.company_name} - {job.title}").strip())
    name = f"{base[:120]}.{format}"
    data, media = (render_docx(found[1]), DOCX) if format == "docx" else (render_pdf(found[1]), PDF)
    return Response(data, media_type=media, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name.replace(' ', '%20')}"})
