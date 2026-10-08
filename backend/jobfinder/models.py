"""ORM models. Imported by alembic/env.py so every table registers on Base.metadata."""
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import false as sa_false
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from jobfinder.db import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")


class User(Base):
    """Invite-only: the owner inserts an email-only row; setting a password (password_hash) activates it."""

    __tablename__ = "users"
    __table_args__ = (CheckConstraint("email = lower(email)", name="ck_users_email_lower"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    digest_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    digest_enabled: Mapped[bool | None] = mapped_column(nullable=True)
    is_admin: Mapped[bool | None] = mapped_column(nullable=True)
    question_emails_enabled: Mapped[bool | None] = mapped_column(nullable=True)
    match_budget_enabled: Mapped[bool | None] = mapped_column(nullable=True)  # False = no daily scoring limit
    match_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)  # LLM-scored jobs per rolling 24h; null = server default
    last_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # throttled to one write per 10 min
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RateEvent(Base):
    __tablename__ = "rate_events"
    __table_args__ = (Index("ix_rate_events_kind_key_created", "kind", "key", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))
    key: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Session(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Profile(Base):
    __tablename__ = "profiles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[dict] = mapped_column(JSONType, default=dict)
    background: Mapped[dict] = mapped_column(JSONType, default=dict)
    # Bumped on every change to status/background/facts; match scores are cached per version.
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Last matching run: {last_run_at, pending, scored}. Shown on the matches page.
    match_summary: Mapped[dict] = mapped_column(JSONType, default=dict, server_default="{}")
    # Recruiter-style audit of the profile (see ai/prompts/profile_audit.md).
    dossier: Mapped[dict] = mapped_column(JSONType, default=dict, server_default="{}")
    readiness: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    audit: Mapped[dict] = mapped_column(JSONType, default=dict, server_default="{}")  # {dimensions, questions, version}
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProfileFact(Base):
    """An atomic answer, e.g. 'Have you used Kubernetes in production?' -> 'Yes, at X'."""

    __tablename__ = "profile_facts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32), default="onboarding")  # onboarding|followup|job_question|interview
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # FK added with the jobs table
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())




class Task(Base):
    """Postgres-backed job queue; claimed with FOR UPDATE SKIP LOCKED."""

    __tablename__ = "tasks"
    __table_args__ = (
        # At most one queued/running task per dedupe_key (NULL keys never conflict).
        Index("uq_tasks_dedupe_active", "dedupe_key", unique=True, postgresql_where=text("status IN ('queued','running')")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|running|done|failed
    result: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    dedupe_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AICall(Base):
    __tablename__ = "ai_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    task: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(16))
    duration_ms: Mapped[int] = mapped_column(Integer)
    ok: Mapped[bool]
    error_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Company(Base):
    """A company whose public ATS board we poll. Seeded from ingest/companies.yaml."""

    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("ats", "slug", name="uq_company_ats_slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    ats: Mapped[str] = mapped_column(String(32))  # greenhouse|lever|ashby
    slug: Mapped[str] = mapped_column(String(200))
    prestige_tier: Mapped[int] = mapped_column(Integer, default=3)  # 1..5
    size: Mapped[str] = mapped_column(String(16), default="mid")  # startup|mid|large
    industry: Mapped[str] = mapped_column(String(100), default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class Job(Base):
    """Cached job posting. One row per (source, external_id)."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_job_source_external"),
        Index("ix_jobs_active_posted", "is_active", "posted_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # greenhouse|lever|ashby|jobspy
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


class CoverLetter(Base):
    """The current cover letter draft for one job, editable by the user."""

    __tablename__ = "cover_letters"
    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_letter_user_job"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text)
    tone: Mapped[str] = mapped_column(String(16), default="professional")
    edited: Mapped[bool] = mapped_column(default=False)  # changed by the user since it was generated
    profile_version: Mapped[int] = mapped_column(Integer, default=1)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ClarifyingQuestion(Base):
    """A question for the candidate that would sharpen their matches; answers become profile facts."""

    __tablename__ = "clarifying_questions"
    __table_args__ = (Index("ix_questions_user_status", "user_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    question: Mapped[str] = mapped_column(Text)
    why: Mapped[str] = mapped_column(Text, default="")
    match_ids: Mapped[list] = mapped_column(JSONType, default=list)  # the matches that raised it
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending|emailed|answered|skipped
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlannerState(Base):
    """What the background planner last decided for a user (shown on the admin schedule page)."""

    __tablename__ = "planner_state"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    last_planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reason: Mapped[str] = mapped_column(String(200), default="", server_default="")
    last_skip: Mapped[str] = mapped_column(String(200), default="", server_default="")
