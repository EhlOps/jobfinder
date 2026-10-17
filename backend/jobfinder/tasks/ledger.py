"""The task ledger: every write to the `tasks` table goes through here.

`add` only flushes: the row commits (or rolls back) with the caller's transaction, so a request that
fails leaves no task behind. Rows that are due now are noted on the session and published to valkey
after it commits (`_after_commit`); a lost publish is re-sent by the scheduler's relay
(`due_for_relay`). Workers `claim` by id, atomically, so a duplicate delivery is a no-op.
"""
import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import event, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from jobfinder.core.clock import utcnow
from jobfinder.core.config import settings
from jobfinder.core.errors import Conflict, NotFound
from jobfinder.models import Task
from jobfinder.tasks.kinds import queue_for

log = logging.getLogger("jobfinder.tasks")
ACTIVE = ("queued", "running")
PENDING = "ledger_publish"  # session.info key: [(queue, kind, task_id)] to publish after commit
RELAY_GRACE = timedelta(seconds=15)  # the after-commit publish gets this long before the relay re-sends
RELAY_RESEND = timedelta(minutes=10)  # a relayed row still queued after this is sent again


def dedupe_key(kind: str, user_id: int | None, payload: dict) -> str:
    """Same key as scheduling.queue.dedupe_key, so legacy and ledger rows dedupe against each other."""
    return hashlib.sha256(f"{kind}|{user_id}|{json.dumps(payload, sort_keys=True, default=str)}".encode()).hexdigest()


async def add(
    db: AsyncSession,
    kind: str,
    payload: dict | None = None,
    *,
    user_id: int | None = None,
    run_after: datetime | None = None,
    dedupe: bool = False,
) -> Task:
    """Queue a task in the caller's transaction (flushes, never commits). With dedupe=True, return the
    queued/running task of the same kind, user and payload instead of adding another; concurrent callers
    converge on one row through the partial unique index."""
    payload, now = payload or {}, utcnow()
    values = {"kind": kind, "queue": queue_for(kind), "payload": payload, "user_id": user_id,
              "run_after": run_after or now, "status": "queued", "attempts": 0}
    if not dedupe:
        task = Task(**values)
        db.add(task)
        await db.flush()
        _note(db, task, now)
        return task
    key = dedupe_key(kind, user_id, payload)
    for _ in range(2):  # the second pass covers the active task finishing between our insert and select
        # A conflicting insert by an open transaction makes this wait for it, so the select below sees it.
        inserted = await db.scalar(
            pg_insert(Task).values(**values, dedupe_key=key)
            .on_conflict_do_nothing(index_elements=["dedupe_key"], index_where=Task.status.in_(ACTIVE))
            .returning(Task.id)
        )
        if inserted:
            task = await db.get(Task, inserted)
            _note(db, task, now)
            return task
        existing = await db.scalar(
            select(Task).where(Task.dedupe_key == key, Task.status.in_(ACTIVE)).order_by(Task.id.desc()).limit(1)
        )
        if existing:
            return existing
    raise RuntimeError("ledger dedupe could not settle")


async def claim(db: AsyncSession, task_id: int) -> Task | None:
    """queued -> running, atomically. None when the row isn't queued and due: another delivery already
    took it, it finished, or it was rescheduled (duplicates are normal; drop them)."""
    now = utcnow()
    claimed = await db.scalar(
        update(Task).where(Task.id == task_id, Task.status == "queued", Task.run_after <= now)
        .values(status="running", locked_at=now, attempts=Task.attempts + 1).returning(Task.id)
    )
    return await db.get(Task, claimed, populate_existing=True) if claimed else None


async def finish(db: AsyncSession, task: Task, result: dict) -> None:
    task.status, task.result, task.error, task.finished_at = "done", result, None, utcnow()
    await db.flush()


async def fail(db: AsyncSession, task: Task, error: str, *, retry: bool) -> None:
    """Record a failure; with retry, queue it again after a backoff (TASK_BACKOFF_SECONDS x4 per attempt:
    30s, 2m, 8m) until TASK_MAX_ATTEMPTS. The relay delivers the retry once it's due."""
    task.error = error[:2000]
    if retry and task.attempts < settings.task_max_attempts:
        task.status, task.sent_at = "queued", None
        task.run_after = utcnow() + timedelta(seconds=settings.task_backoff_seconds * 4 ** (task.attempts - 1))
    else:
        task.status, task.finished_at = "failed", utcnow()
    await db.flush()


async def heartbeat(db: AsyncSession, task_id: int) -> None:
    """Called periodically while a task runs so `reap` can tell it from one whose worker died."""
    await db.execute(update(Task).where(Task.id == task_id, Task.status == "running").values(locked_at=utcnow()))


async def requeue(db: AsyncSession, task_id: int) -> Task:
    """Run a finished or failed task again from scratch (the admin's retry). Raises Conflict while it's
    still active or an identical task is, NotFound when there's no such task."""
    task = await db.get(Task, task_id)
    if task is None:
        raise NotFound(f"No task {task_id}")
    if task.status in ACTIVE:
        raise Conflict(f"task {task_id} is already {task.status}")
    if task.dedupe_key and await db.scalar(
        select(Task.id).where(Task.dedupe_key == task.dedupe_key, Task.status.in_(ACTIVE))
    ):
        raise Conflict(f"an identical {task.kind} task is already queued or running")
    now = utcnow()
    task.status, task.attempts, task.error, task.result = "queued", 0, None, None
    task.run_after, task.finished_at, task.locked_at, task.sent_at = now, None, None, None
    await db.flush()
    _note(db, task, now)
    return task


async def reap(db: AsyncSession) -> int:
    """Recover tasks whose worker died mid-run (no heartbeat for TASK_STUCK_AFTER_MINUTES): queue them
    again for the relay, or fail them once they used all their attempts. Returns the number requeued."""
    attempts = settings.task_max_attempts
    stale = (Task.status == "running", Task.locked_at < utcnow() - timedelta(minutes=settings.task_stuck_after_minutes))
    failed = await db.execute(
        update(Task).where(*stale, Task.attempts >= attempts)
        .values(status="failed", finished_at=utcnow(), error=f"worker stopped responding during {attempts} attempts")
    )
    requeued = await db.execute(update(Task).where(*stale).values(status="queued", sent_at=None))
    if failed.rowcount:
        log.warning("failed %d task(s) that kept timing out", failed.rowcount)
    return requeued.rowcount or 0


async def due_for_relay(db: AsyncSession, now: datetime, *, limit: int = 500) -> list[tuple[int, str, str]]:
    """(id, queue, kind) of queued, due rows the relay should publish: never relayed and older than the
    after-commit publish's grace, or relayed more than RELAY_RESEND ago and still queued."""
    resend = now - RELAY_RESEND
    rows = await db.execute(
        select(Task.id, Task.queue, Task.kind)
        .where(Task.status == "queued", Task.run_after <= now,
               or_(Task.sent_at.is_(None) & (Task.created_at < now - RELAY_GRACE), Task.sent_at < resend))
        .order_by(Task.run_after, Task.id).limit(limit)
    )
    return [tuple(r) for r in rows]


async def mark_sent(db: AsyncSession, ids: list[int], now: datetime) -> None:
    if ids:
        await db.execute(update(Task).where(Task.id.in_(ids)).values(sent_at=now))


# ---------------------------------------------------------------- publish after commit

_publishing: set[asyncio.Task] = set()  # strong refs until each publish finishes


def _note(db: AsyncSession, task: Task, now: datetime) -> None:
    if task.run_after <= now:  # later rows wait for the relay
        db.info.setdefault(PENDING, []).append((task.queue, task.kind, task.id))


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    pending = session.info.pop(PENDING, None)
    if not pending or settings.tasks_backend != "ledger":
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # a sync session outside the loop: the relay will send them
        return
    t = loop.create_task(_publish(pending))
    _publishing.add(t)
    t.add_done_callback(_publishing.discard)


@event.listens_for(Session, "after_soft_rollback")
def _after_rollback(session: Session, previous_transaction) -> None:
    if previous_transaction.parent is None:  # the whole transaction, not a savepoint
        session.info.pop(PENDING, None)


async def _publish(pending: list[tuple[str, str, int]]) -> None:
    from jobfinder.tasks import brokers

    for queue, kind, task_id in pending:
        try:
            await brokers.publish(queue, kind, task_id)
        except Exception as e:  # valkey down: the row stays queued and the relay re-sends it
            log.warning("publishing task %s (%s) failed, the relay will retry: %s", task_id, kind, e)
