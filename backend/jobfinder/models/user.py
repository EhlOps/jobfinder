"""Accounts: users, their login sessions, and rate-limit events."""
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from jobfinder.core.db import Base


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
    is_admin: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    question_emails_enabled: Mapped[bool | None] = mapped_column(nullable=True)
    match_budget_enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))  # False = no daily scoring limit
    match_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)  # LLM-scored jobs per rolling 24h; null = server default
    last_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # throttled to one write per 10 min
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Session(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RateEvent(Base):
    __tablename__ = "rate_events"
    __table_args__ = (Index("ix_rate_events_kind_key_created", "kind", "key", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))
    key: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
