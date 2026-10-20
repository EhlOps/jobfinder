"""Minimal Postgres task queue: enqueue, claim (SKIP LOCKED), heartbeat, complete, fail with backoff."""
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.core.clock import utcnow
from jobfinder.models import Task
from jobfinder.tasks import ledger
from jobfinder.tasks.ledger import dedupe_key  # the one key, shared with ledger rows

# Retry policy from settings: TASK_MAX_ATTEMPTS, TASK_BACKOFF_SECONDS (x4 per attempt) and
# TASK_STUCK_AFTER_MINUTES (the worker heartbeats every TASK_HEARTBEAT_SECONDS while a task runs).
ACTIVE = ("queued", "running")
RENDER_PREFIX = "render_"  # tasks for the opt-in browser container (jobfinder.renderer), never the worker


def _now() -> datetime:
    return utcnow()


async def enqueue(
    db: AsyncSession,
    kind: str,
    payload: dict | None = None,
    *,
    user_id: int | None = None,
    run_after: datetime | None = None,
    dedupe: bool = False,
) -> Task:
    """With dedupe=True, reuse a queued/running task of the same kind, user and payload.
    Atomic: a partial unique index on queued dedupe_keys makes concurrent callers converge on one task."""
    payload = payload or {}
    if not dedupe:
        task = Task(kind=kind, payload=payload, user_id=user_id, run_after=run_after or _now())
        db.add(task)
        await db.commit()
        return task
    key = dedupe_key(kind, user_id, payload)
    for _ in range(2):  # the second pass covers the active task finishing between our insert and select
        twin = select(Task).where(Task.dedupe_key == key).order_by(Task.id.desc()).limit(1)
        if running := await db.scalar(twin.where(Task.status == "running")):
            return running
        inserted = await db.scalar(
            pg_insert(Task)
            .values(kind=kind, payload=payload, user_id=user_id, run_after=run_after or _now(), dedupe_key=key,
                    status="queued", attempts=0)
            .on_conflict_do_nothing(index_elements=["dedupe_key"], index_where=Task.status == "queued")
            .returning(Task.id)
        )
        await db.commit()
        existing = await db.scalar(
            select(Task).where(Task.id == inserted) if inserted
            else twin.where(Task.status.in_(ACTIVE))
        )
        if existing:
            return existing
    raise RuntimeError("enqueue dedupe could not settle")


async def claim(db: AsyncSession, *, kind_prefix: str | None = None, exclude_prefix: str | None = None
                ) -> Task | None:
    """The next due task. `kind_prefix` claims only kinds starting with it; `exclude_prefix` skips them."""
    # Not while a twin runs: a rerun queued by ledger.add waits for it (claimed by a later poll).
    where = [Task.status == "queued", Task.run_after <= _now(), ~ledger.has_twin("running")]
    if kind_prefix:
        where.append(Task.kind.startswith(kind_prefix, autoescape=True))
    if exclude_prefix:
        where.append(~Task.kind.startswith(exclude_prefix, autoescape=True))
    task = (
        await db.execute(
            select(Task)
            .where(*where)
            .order_by(Task.run_after, Task.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if task:
        task.status = "running"
        task.locked_at = _now()
        task.attempts += 1
    await db.commit()
    return task


async def complete(db: AsyncSession, task: Task, result: dict) -> None:
    task.status, task.result, task.error, task.finished_at = "done", result, None, _now()
    await db.commit()


async def fail(db: AsyncSession, task: Task, error: str, *, retry: bool) -> None:
    await ledger.fail(db, task, error, retry=retry)  # same backoff; a queued rerun replaces the retry
    await db.commit()


async def heartbeat(db: AsyncSession, task_id: int) -> None:
    """Called periodically while a task runs so the reaper can tell it from one whose worker died."""
    await db.execute(update(Task).where(Task.id == task_id, Task.status == "running").values(locked_at=_now()))
    await db.commit()


async def reap_stuck(db: AsyncSession) -> int:
    """Recover tasks whose worker died mid-run: requeue them, or fail them once they used all their attempts.
    Returns the number requeued."""
    requeued = await ledger.reap(db)
    await db.commit()
    return requeued
