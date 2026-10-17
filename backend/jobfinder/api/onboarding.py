"""Kick off the slow AI steps of onboarding. Each returns a task id the UI polls."""

from fastapi import APIRouter

from jobfinder.api.deps import CurrentUser, DbSession, Store
from jobfinder.schemas.common import TaskRef
from jobfinder.services import profile

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


@router.post("/extract", response_model=TaskRef, status_code=202)
async def start_extract(user: CurrentUser, db: DbSession, store: Store):
    task = await profile.start_extract(db, store, user.id)
    return TaskRef(task_id=task.id)


@router.post("/followups", response_model=TaskRef, status_code=202)
async def start_followups(user: CurrentUser, db: DbSession):
    task = await profile.start_followups(db, user.id)
    return TaskRef(task_id=task.id)
