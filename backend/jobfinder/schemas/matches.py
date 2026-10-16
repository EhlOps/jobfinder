"""The match feed: a scored job, its detail, the paged list, and the bodies that act on a match."""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from jobfinder.schemas.common import HttpUrlStr

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
    url: HttpUrlStr
    posted_at: datetime | None
    is_active: bool
    sponsorship: Literal["sponsors", "refuses", "likely", "unlikely"] | None = None  # None = unknown


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


class JobAnswer(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=3000)


class JobAnswersIn(BaseModel):
    answers: list[JobAnswer] = Field(min_length=1, max_length=14)
