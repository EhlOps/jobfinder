"""Kick off the slow AI steps of onboarding. Each returns a task id the UI polls."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.auth.security import current_user
from jobfinder.db import get_db
from jobfinder.models import Profile, User
from jobfinder.scheduling import queue
from jobfinder.storage.documents import DocumentStore, get_store, is_tailored

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


class TaskRef(BaseModel):
    task_id: int


@router.post("/extract", response_model=TaskRef, status_code=202)
async def start_extract(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    store: Annotated[DocumentStore, Depends(get_store)],
):
    if not any(d.chars > 0 and not is_tailored(d) for d in store.list(user.id)):
        raise HTTPException(422, "Upload a resume or add a link we can read first")
    task = await queue.enqueue(db, "extract_profile", user_id=user.id, dedupe=True)
    return TaskRef(task_id=task.id)


@router.post("/followups", response_model=TaskRef, status_code=202)
async def start_followups(
    user: Annotated[User, Depends(current_user)], db: Annotated[AsyncSession, Depends(get_db)]
):
    profile = await db.get(Profile, user.id)
    if not profile or not profile.background:
        raise HTTPException(422, "Save your background first")
    task = await queue.enqueue(db, "followup_questions", user_id=user.id, dedupe=True)
    return TaskRef(task_id=task.id)
