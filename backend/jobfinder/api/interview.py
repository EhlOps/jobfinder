"""'Strengthen my profile': the recruiter-style audit, its next questions, and answering them."""
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.auth.security import current_user
from jobfinder.db import get_db
from jobfinder.models import Profile, User
from jobfinder.profile import interview
from jobfinder.scheduling import queue

router = APIRouter(prefix="/api/profile/interview", tags=["interview"])
DB = Annotated[AsyncSession, Depends(get_db)]


class InterviewOut(BaseModel):
    readiness: int
    dimensions: dict[str, int]
    dossier: dict[str, Any]
    questions: list[dict[str, Any]]
    audited: bool
    stale: bool  # the profile changed since the last audit


class AnswerIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(default="", max_length=interview.MAX_ANSWER_CHARS)
    skip: bool = False


class AnswersIn(BaseModel):
    answers: list[AnswerIn] = Field(max_length=20)


class TaskRef(BaseModel):
    task_id: int


@router.get("", response_model=InterviewOut)
async def read_interview(user: Annotated[User, Depends(current_user)], db: DB):
    return interview.view(await db.get(Profile, user.id))


@router.post("/refresh", response_model=TaskRef, status_code=202)
async def refresh(user: Annotated[User, Depends(current_user)], db: DB):
    profile = await db.get(Profile, user.id)
    if not profile or not (profile.background or profile.status):
        raise HTTPException(422, "Set up your profile first")
    task = await queue.enqueue(db, "audit_profile", user_id=user.id, dedupe=True)
    return TaskRef(task_id=task.id)


@router.post("/answers")
async def answer(body: AnswersIn, user: Annotated[User, Depends(current_user)], db: DB):
    return await interview.answer(db, user.id, [a.model_dump() for a in body.answers])
