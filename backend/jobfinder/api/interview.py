"""'Strengthen my profile': the recruiter-style audit, its next questions, and answering them."""
from fastapi import APIRouter

from jobfinder.api.deps import CurrentUser, DbSession
from jobfinder.schemas.common import TaskRef
from jobfinder.schemas.interview import InterviewAnswersIn, InterviewOut
from jobfinder.services import interview

router = APIRouter(prefix="/api/profile/interview", tags=["interview"])


@router.get("", response_model=InterviewOut)
async def read_interview(user: CurrentUser, db: DbSession):
    return await interview.current(db, user.id)


@router.post("/refresh", response_model=TaskRef, status_code=202)
async def refresh(user: CurrentUser, db: DbSession):
    task = await interview.request_audit(db, user.id)
    return TaskRef(task_id=task.id)


@router.post("/answers")
async def answer(body: InterviewAnswersIn, user: CurrentUser, db: DbSession):
    return await interview.answer(db, user.id, [a.model_dump() for a in body.answers])
