"""Clarifying questions for the candidate."""
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from jobfinder.core.db import Base
from jobfinder.models.base import JSONType


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
