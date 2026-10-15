"""Minimal Postgres task queue: enqueue, claim (SKIP LOCKED), heartbeat, complete, fail with backoff."""
import hashlib
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.core.clock import utcnow
from jobfinder.core.config import settings
from jobfinder.models import Task

log = logging.getLogger("jobfinder.queue")
# Retry policy from settings: TASK_MAX_ATTEMPTS, TASK_BACKOFF_SECONDS (x4 per attempt) and
# TASK_STUCK_AFTER_MINUTES (the worker heartbeats every TASK_HEARTBEAT_SECONDS while a task runs).
ACTIVE = ("queued", "running")
RENDER_PREFIX = "render_"  # tasks for the opt-in browser container (jobfinder.renderer), never the worker


def _now() -> datetime:
    return utcnow()


def dedupe_key(kind: str, user_id: int | None, payload: dict) -> str:
    return hashlib.sha256(f"{kind}|{user_id}|{json.dumps(payload, sort_keys=True, default=str)}".encode()).hexdigest()


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
    Atomic: a partial unique index on dedupe_key makes concurrent callers converge on one task."""
    payload = payload or {}
    if not dedupe:
        task = Task(kind=kind, payload=payload, user_id=user_id, run_after=run_after or _now())
        db.add(task)
        await db.commit()
        return task
    key = dedupe_key(kind, user_id, payload)
    for _ in range(2):  # the second pass covers the active task finishing between our insert and select
        inserted = await db.scalar(
            pg_insert(Task)
            .values(kind=kind, payload=payload, user_id=user_id, run_after=run_after or _now(), dedupe_key=key,
                    status="queued", attempts=0)
            .on_conflict_do_nothing(index_elements=["dedupe_key"], index_where=Task.status.in_(ACTIVE))
            .returning(Task.id)
        )
        await db.commit()
        existing = await db.scalar(
            select(Task).where(Task.id == inserted) if inserted
            else select(Task).where(Task.dedupe_key == key, Task.status.in_(ACTIVE)).order_by(Task.id.desc()).limit(1)
        )
        if existing:
            return existing
    raise RuntimeError("enqueue dedupe could not settle")


async def claim(db: AsyncSession, *, kind_prefix: str | None = None, exclude_prefix: str | None = None
                ) -> Task | None:
    """The next due task. `kind_prefix` claims only kinds starting with it; `exclude_prefix` skips them."""
    where = [Task.status == "queued", Task.run_after <= _now()]
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
    task.error = error[:2000]
    if retry and task.attempts < settings.task_max_attempts:
        task.status = "queued"
        backoff = settings.task_backoff_seconds * 4 ** (task.attempts - 1)  # 30s, 2m, 8m by default
        task.run_after = _now() + timedelta(seconds=backoff)
    else:
        task.status, task.finished_at = "failed", _now()
    await db.commit()


async def heartbeat(db: AsyncSession, task_id: int) -> None:
    """Called periodically while a task runs so the reaper can tell it from one whose worker died."""
    await db.execute(update(Task).where(Task.id == task_id, Task.status == "running").values(locked_at=_now()))
    await db.commit()


async def reap_stuck(db: AsyncSession) -> int:
    """Recover tasks whose worker died mid-run: requeue them, or fail them once they used all their attempts.
    Returns the number requeued."""
    attempts = settings.task_max_attempts
    stale = (Task.status == "running", Task.locked_at < _now() - timedelta(minutes=settings.task_stuck_after_minutes))
    failed = await db.execute(
        update(Task).where(*stale, Task.attempts >= attempts)
        .values(status="failed", finished_at=_now(), error=f"worker stopped responding during {attempts} attempts")
    )
    requeued = await db.execute(update(Task).where(*stale).values(status="queued"))
    await db.commit()
    if failed.rowcount:
        log.warning("failed %d task(s) that kept timing out", failed.rowcount)
    return requeued.rowcount or 0
