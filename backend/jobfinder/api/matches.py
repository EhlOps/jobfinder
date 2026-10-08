from datetime import datetime
from typing import Annotated, Any, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.auth.security import current_user
from jobfinder.db import get_db
from jobfinder.ingest.base import safe_http_url
from jobfinder.matching import prefilter
from jobfinder.models import CoverLetter, Job, JobMatch, Profile, ProfileFact, User
from jobfinder.scheduling import queue

router = APIRouter(prefix="/api/matches", tags=["matches"])

DB = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(current_user)]
Status = Literal["new", "saved", "applied", "dismissed"]


class JobOut(BaseModel):
    id: int
    title: str
    company: str
    location: str
    workplace_type: str | None
    seniority: str | None
    salary_min: int | None
    salary_max: int | None
    salary_currency: str | None
    url: str
    posted_at: datetime | None
    is_active: bool

    @field_validator("url")
    @classmethod
    def _http_only(cls, v: str) -> str:
        return safe_http_url(v)  # also covers rows stored before ingestion started checking


class MatchOut(BaseModel):
    id: int
    status: Status
    score: int
    confidence: float
    verdict: str
    reasons: list[str]
    gaps: list[str]
    hire_verdict: str = ""
    recruiter_take: str = ""
    stale: bool  # scored against an older version of the profile
    has_cover_letter: bool = False
    scored_at: datetime
    job: JobOut


class MatchDetail(MatchOut):
    unknowns: list[dict[str, Any]]
    requirements: list[dict[str, Any]] = []
    description: str


class MatchList(BaseModel):
    items: list[MatchOut]
    total: int
    counts: dict[str, int]
    summary: dict[str, Any]


class StatusIn(BaseModel):
    status: Status


class TaskRef(BaseModel):
    task_id: int


def _job_out(j: Job) -> JobOut:
    return JobOut(
        id=j.id, title=j.title, company=j.company_name, location=j.location, workplace_type=j.workplace_type,
        seniority=j.seniority, salary_min=j.salary_min, salary_max=j.salary_max,
        salary_currency=j.salary_currency, url=j.url, posted_at=j.posted_at, is_active=j.is_active,
    )


def _out(m: JobMatch, j: Job, version: int, has_letter: bool = False) -> dict:
    return {
        "id": m.id, "status": m.status, "score": m.llm_score, "confidence": m.confidence,
        "verdict": m.verdict, "reasons": m.reasons, "gaps": m.gaps,
        "hire_verdict": m.hire_verdict, "recruiter_take": m.recruiter_take,
        "stale": m.profile_version < version, "has_cover_letter": has_letter, "scored_at": m.scored_at,
        "job": _job_out(j),
    }


def _visible(user_id: int):
    """Closed jobs disappear from the feed unless the user saved or applied to them."""
    return sa.and_(
        JobMatch.user_id == user_id,
        sa.or_(Job.is_active.is_(True), JobMatch.status.in_(("saved", "applied"))),
    )


@router.get("", response_model=MatchList)
async def list_matches(
    user: CurrentUser,
    db: DB,
    status: Annotated[Status | None, Query(description="omit for new + saved")] = None,
    min_score: Annotated[int, Query(ge=0, le=100)] = 0,
    workplace: Literal["remote", "hybrid", "onsite"] | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    early_career: Annotated[bool, Query(description="internships, new-grad and early-career roles only")] = False,
    sort: Literal["score", "recent"] = "score",
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    profile = await db.get(Profile, user.id)
    version = profile.version if profile else 1
    base = sa.select(JobMatch, Job).join(Job, Job.id == JobMatch.job_id).where(_visible(user.id))

    counts = {s: 0 for s in ("new", "saved", "applied", "dismissed")}
    rows = await db.execute(
        sa.select(JobMatch.status, sa.func.count()).join(Job, Job.id == JobMatch.job_id).where(_visible(user.id)).group_by(JobMatch.status)
    )
    counts.update({s: n for s, n in rows.all()})

    filters = [JobMatch.status == status] if status else [JobMatch.status.in_(("new", "saved"))]
    filters.append(JobMatch.llm_score >= min_score)
    if workplace:
        filters.append(Job.workplace_type == workplace)
    if early_career:
        filters.append(sa.or_(
            Job.seniority.in_(("intern", "new_grad")), Job.title.op("~*")(prefilter.EARLY_CAREER_SQL),
            Job.title.op("~*")(r"\y(intern|internship|co-?op)\y"),
        ))
    if q:
        like = f"%{q.strip()}%"
        filters.append(sa.or_(Job.title.ilike(like), Job.company_name.ilike(like)))

    total = await db.scalar(
        sa.select(sa.func.count()).select_from(JobMatch).join(Job, Job.id == JobMatch.job_id).where(_visible(user.id), *filters)
    )
    order = (
        [JobMatch.llm_score.desc(), JobMatch.id.desc()] if sort == "score"
        else [sa.func.coalesce(Job.posted_at, JobMatch.scored_at).desc(), JobMatch.id.desc()]
    )
    page = (await db.execute(base.where(*filters).order_by(*order).limit(limit).offset(offset))).all()
    with_letters = await _with_letters(db, user.id, [j.id for _, j in page])
    return MatchList(
        items=[MatchOut(**_out(m, j, version, j.id in with_letters)) for m, j in page],
        total=total or 0, counts=counts,
        summary={**((profile.match_summary if profile else {}) or {}), "career_stage": prefilter.career_stage((profile.status if profile else {}) or {})},
    )


async def _with_letters(db: AsyncSession, user_id: int, job_ids: list[int]) -> set[int]:
    if not job_ids:
        return set()
    rows = await db.scalars(sa.select(CoverLetter.job_id).where(CoverLetter.user_id == user_id, CoverLetter.job_id.in_(job_ids)))
    return set(rows)


async def _get_owned(db: AsyncSession, user: User, match_id: int) -> tuple[JobMatch, Job]:
    row = (
        await db.execute(
            sa.select(JobMatch, Job).join(Job, Job.id == JobMatch.job_id).where(JobMatch.id == match_id, JobMatch.user_id == user.id)
        )
    ).first()
    if not row:
        raise HTTPException(404, "Not found")
    return row


@router.get("/{match_id}", response_model=MatchDetail)
async def get_match(match_id: int, user: CurrentUser, db: DB):
    m, j = await _get_owned(db, user, match_id)
    profile = await db.get(Profile, user.id)
    has_letter = j.id in await _with_letters(db, user.id, [j.id])
    return MatchDetail(**_out(m, j, profile.version if profile else 1, has_letter), unknowns=m.unknowns, requirements=m.requirements or [], description=j.description_text)


class JobAnswer(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=3000)


class JobAnswersIn(BaseModel):
    answers: list[JobAnswer] = Field(min_length=1, max_length=14)


@router.post("/{match_id}/answers", status_code=202)
async def answer_for_match(match_id: int, body: JobAnswersIn, user: CurrentUser, db: DB):
    """Answer a requirement question right on the job: saved as profile facts, then this and the other
    matches are re-scored (this one first)."""
    m, j = await _get_owned(db, user, match_id)
    for a in body.answers:
        db.add(ProfileFact(user_id=user.id, question=a.question, answer=a.answer, source="job_question", job_id=j.id))
    profile = await db.get(Profile, user.id)
    if profile:
        profile.version += 1
    await db.commit()
    await queue.enqueue(db, "match_user", user_id=user.id, dedupe=True)
    return {"saved": len(body.answers)}


@router.patch("/{match_id}", response_model=MatchOut)
async def set_status(match_id: int, body: StatusIn, user: CurrentUser, db: DB):
    m, j = await _get_owned(db, user, match_id)
    m.status = body.status
    await db.commit()
    profile = await db.get(Profile, user.id)
    has_letter = j.id in await _with_letters(db, user.id, [j.id])
    return MatchOut(**_out(m, j, profile.version if profile else 1, has_letter))


@router.post("/refresh", response_model=TaskRef, status_code=202)
async def refresh(user: CurrentUser, db: DB):
    """Find and score new matches now (runs in the worker)."""
    profile = await db.get(Profile, user.id)
    if not profile or not profile.status:
        raise HTTPException(422, "Tell us about your search first (onboarding)")
    task = await queue.enqueue(db, "match_user", user_id=user.id, dedupe=True)
    return TaskRef(task_id=task.id)
