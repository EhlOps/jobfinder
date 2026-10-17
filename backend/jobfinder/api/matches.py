"""The match feed and one match: list, read, set status, answer its questions, and re-score."""
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from jobfinder.api.deps import CurrentUser, DbSession
from jobfinder.schemas.common import TaskRef
from jobfinder.schemas.matches import (
    JobAnswersIn,
    MatchDetail,
    MatchList,
    MatchOut,
    Status,
    StatusIn,
)
from jobfinder.services import matches as service

router = APIRouter(prefix="/api/matches", tags=["matches"])


@router.get("", response_model=MatchList)
async def list_matches(
    user: CurrentUser,
    db: DbSession,
    status: Annotated[Status | None, Query(description="omit for new + saved")] = None,
    min_score: Annotated[int, Query(ge=0, le=100)] = 0,
    workplace: Literal["remote", "hybrid", "onsite"] | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    early_career: Annotated[bool, Query(description="internships, new-grad and early-career roles only")] = False,
    sort: Literal["score", "recent"] = "score",
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return await service.feed(db, user.id, service.FeedQuery(
        status=status, min_score=min_score, workplace=workplace, q=q, early_career=early_career,
        sort=sort, limit=limit, offset=offset,
    ))


@router.get("/{match_id}", response_model=MatchDetail)
async def get_match(match_id: int, user: CurrentUser, db: DbSession):
    return await service.detail(db, user.id, match_id)


@router.post("/{match_id}/answers", status_code=202)
async def answer_for_match(match_id: int, body: JobAnswersIn, user: CurrentUser, db: DbSession):
    """Answer a requirement question right on the job: saved as profile facts, then this and the other
    matches are re-scored (this one first)."""
    return await service.answer(db, user.id, match_id, [(a.question, a.answer) for a in body.answers])


@router.patch("/{match_id}", response_model=MatchOut)
async def set_status(match_id: int, body: StatusIn, user: CurrentUser, db: DbSession):
    return await service.set_status(db, user.id, match_id, body.status)


@router.post("/refresh", response_model=TaskRef, status_code=202)
async def refresh(user: CurrentUser, db: DbSession):
    """Find and score new matches now (runs in the worker)."""
    return await service.refresh(db, user.id)
