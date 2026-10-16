"""Job postings, their per-user matches, and the job links users paste."""
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import false as sa_false
from sqlalchemy.orm import Mapped, mapped_column

from jobfinder.core.db import Base
from jobfinder.models.base import JSONType


class Job(Base):
    """Cached job posting. One row per (source, external_id)."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_job_source_external"),
        Index("ix_jobs_active_posted", "is_active", "posted_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # an ATS_FETCHERS key, jobspy, or usajobs
    external_id: Mapped[str] = mapped_column(String(200))
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    company_name: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(500))
    location: Mapped[str] = mapped_column(String(500), default="")
    workplace_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # remote|hybrid|onsite
    employment_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    seniority: Mapped[str | None] = mapped_column(String(16), nullable=True)
    salary_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    description_text: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(1024))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    is_active: Mapped[bool] = mapped_column(default=True)
    # sha1(company|title|location), normalised: the same posting seen via two sources collapses.
    dedupe_hash: Mapped[str] = mapped_column(String(40), index=True)


class JobMatch(Base):
    """A job that has been scored against one user's profile."""

    __tablename__ = "job_matches"
    __table_args__ = (
        UniqueConstraint("user_id", "job_id", name="uq_match_user_job"),
        Index("ix_matches_user_status_score", "user_id", "status", "llm_score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    profile_version: Mapped[int] = mapped_column(Integer)  # version the score was computed against
    prefilter_score: Mapped[float] = mapped_column(Float, default=0)
    llm_score: Mapped[int] = mapped_column(Integer)  # 0..100
    confidence: Mapped[float] = mapped_column(Float)  # 0..1: how much evidence the profile gave the model
    verdict: Mapped[str] = mapped_column(String(16))  # strong|good|stretch|no (derived from score)
    reasons: Mapped[list] = mapped_column(JSONType, default=list)
    gaps: Mapped[list] = mapped_column(JSONType, default=list)
    unknowns: Mapped[list] = mapped_column(JSONType, default=list)  # [{question, why}] for milestone 7 emails
    requirements: Mapped[list] = mapped_column(JSONType, default=list, server_default="[]")  # [RequirementCheck]
    hire_verdict: Mapped[str] = mapped_column(String(8), default="", server_default="")  # yes|maybe|no
    recruiter_take: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(String(16), default="new")  # new|saved|applied|dismissed
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    digested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # True once this match's `unknowns` have been looked at for clarifying questions; reset on re-score.
    questions_collected: Mapped[bool] = mapped_column(default=False, server_default=sa_false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JobLink(Base):
    """A job URL a user pasted. The board it resolves to is shared with everyone; the link itself is private."""

    __tablename__ = "job_links"
    __table_args__ = (
        UniqueConstraint("user_id", "url", name="uq_job_link_user_url"),
        Index("ix_job_links_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(String(16), default="resolving")  # resolving|resolved|unresolved
    ats: Mapped[str | None] = mapped_column(String(32), nullable=True)
    slug: Mapped[str | None] = mapped_column(String(200), nullable=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    rung: Mapped[str | None] = mapped_column(String(32), nullable=True)  # which resolver rung found the board
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
