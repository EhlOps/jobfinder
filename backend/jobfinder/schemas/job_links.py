"""Pasted job links: the link, how resolving it went, and the board's roles it led to."""
from datetime import datetime

from pydantic import BaseModel, Field

from jobfinder.schemas.common import HttpUrlStr


class JobLinkIn(BaseModel):
    url: str = Field(max_length=4096)  # clean_url enforces the real limit with a readable message


class JobLinkOut(BaseModel):
    id: int
    url: HttpUrlStr
    status: str
    company_id: int | None
    company: str | None
    ats: str | None
    rung: str | None
    error: str | None
    job_count: int
    pasted_job_id: int | None
    created_at: datetime
    resolved_at: datetime | None


class LinkJob(BaseModel):
    id: int
    title: str
    location: str
    url: HttpUrlStr
    posted_at: datetime | None
    match_id: int | None
    score: int | None


class LinkDetail(JobLinkOut):
    jobs: list[LinkJob]
