"""Background work: the task queue, the AI call log, and per-user planner state."""
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from jobfinder.core.db import Base
from jobfinder.models.base import JSONType


class Task(Base):
    """The task ledger: the truth for every background task (tasks.ledger). valkey only delivers ids."""

    __tablename__ = "tasks"
    __table_args__ = (
        # At most one queued task per dedupe_key (NULL keys never conflict); a running twin may coexist,
        # so a rerun asked for mid-run waits behind it (tasks.ledger.add/claim).
        Index("uq_tasks_dedupe_queued", "dedupe_key", unique=True, postgresql_where=text("status = 'queued'")),
        # The scheduler's relay: due queued rows in (run_after, id) order (tasks.ledger.due_for_relay).
        Index("ix_tasks_due", "run_after", "id", postgresql_where=text("status = 'queued'")),
        # Maintenance's prune: finished rows by age (tasks.ledger.prune).
        Index("ix_tasks_finished", "finished_at", postgresql_where=text("status IN ('done', 'failed')")),
        # The admin schedule's Queues table: rows claimed in the last day (repositories.admin.lane_stats).
        Index("ix_tasks_started", "started_at", postgresql_where=text("started_at IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(64))
    # The valkey stream that delivers it (tasks.kinds.QUEUES): priority | ai | io | render.
    queue: Mapped[str] = mapped_column(String(16), default="io", server_default="io")
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|running|done|failed
    result: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    dedupe_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the scheduler's relay last published it; NULL until then (the api's after-commit publish
    # doesn't record one). The relay re-sends due rows that stay queued.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the latest attempt was claimed (ledger.claim); NULL until then, and on rows claimed before 0027.
    # started_at - run_after is that attempt's wait, finished_at - started_at its run.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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


class PlannerState(Base):
    """What the background planner last decided for a user (shown on the admin schedule page)."""

    __tablename__ = "planner_state"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    last_planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reason: Mapped[str] = mapped_column(String(200), default="", server_default="")
    last_skip: Mapped[str] = mapped_column(String(200), default="", server_default="")
