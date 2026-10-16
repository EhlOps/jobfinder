"""Kick off the slow AI steps of onboarding. Each returns a task id the UI polls."""

from fastapi import APIRouter, HTTPException

from jobfinder.api.deps import CurrentUser, DbSession, Store
from jobfinder.models import Profile
from jobfinder.scheduling import queue
from jobfinder.schemas.common import TaskRef
from jobfinder.storage.documents import is_tailored

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


@router.post("/extract", response_model=TaskRef, status_code=202)
async def start_extract(
    user: CurrentUser,
    db: DbSession,
    store: Store,
):
    if not any(d.chars > 0 and not is_tailored(d) for d in store.list(user.id)):
        raise HTTPException(422, "Upload a resume or add a link we can read first")
    task = await queue.enqueue(db, "extract_profile", user_id=user.id, dedupe=True)
    return TaskRef(task_id=task.id)


@router.post("/followups", response_model=TaskRef, status_code=202)
async def start_followups(
    user: CurrentUser, db: DbSession
):
    profile = await db.get(Profile, user.id)
    if not profile or not profile.background:
        raise HTTPException(422, "Save your background first")
    task = await queue.enqueue(db, "followup_questions", user_id=user.id, dedupe=True)
    return TaskRef(task_id=task.id)
