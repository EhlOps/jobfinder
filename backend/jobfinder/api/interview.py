"""'Strengthen my profile': the recruiter-style audit, its next questions, and answering them."""
from fastapi import APIRouter, HTTPException

from jobfinder.api.deps import CurrentUser, DbSession
from jobfinder.models import Profile
from jobfinder.profile import interview
from jobfinder.scheduling import queue
from jobfinder.schemas.common import TaskRef
from jobfinder.schemas.interview import InterviewAnswersIn, InterviewOut

router = APIRouter(prefix="/api/profile/interview", tags=["interview"])


@router.get("", response_model=InterviewOut)
async def read_interview(user: CurrentUser, db: DbSession):
    return interview.view(await db.get(Profile, user.id))


@router.post("/refresh", response_model=TaskRef, status_code=202)
async def refresh(user: CurrentUser, db: DbSession):
    profile = await db.get(Profile, user.id)
    if not profile or not (profile.background or profile.status):
        raise HTTPException(422, "Set up your profile first")
    task = await queue.enqueue(db, "audit_profile", user_id=user.id, dedupe=True)
    return TaskRef(task_id=task.id)


@router.post("/answers")
async def answer(body: InterviewAnswersIn, user: CurrentUser, db: DbSession):
    return await interview.answer(db, user.id, [a.model_dump() for a in body.answers])
