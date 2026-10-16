"""A user's profile and the atomic facts gathered for it."""
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from jobfinder.core.db import Base
from jobfinder.models.base import JSONType


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
    career_stage: Mapped[str] = mapped_column(String(16), default="", server_default="")  # see matching.prefilter.career_stage
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
