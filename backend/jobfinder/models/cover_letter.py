"""Per-job cover letter drafts."""
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from jobfinder.core.db import Base


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
