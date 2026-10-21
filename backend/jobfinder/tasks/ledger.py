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
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy import case, delete, event, exists, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, aliased

from jobfinder.core.clock import utcnow
from jobfinder.core.config import settings
from jobfinder.core.context import default_context
from jobfinder.core.errors import Conflict, NotFound
from jobfinder.models import Task
from jobfinder.tasks.kinds import QUEUES, queue_for

log = logging.getLogger("jobfinder.tasks")
ACTIVE = ("queued", "running")
PENDING = "ledger_publish"  # session.info key: [(queue, kind, task_id)] to publish after commit
RELAY_GRACE = timedelta(seconds=15)  # the after-commit publish gets this long before the relay re-sends
RELAY_RESEND = timedelta(minutes=10)  # a relayed row still queued after this is sent again
DEDUPE_INDEX = "uq_tasks_dedupe_queued"  # at most one queued task per dedupe_key
TWIN_WAIT = timedelta(seconds=30)  # how long `claim` defers a rerun whose twin is still running
FINISHED = ("done", "failed")
PRUNE_BATCH = 5000  # rows one prune deletes, so a large backlog is cleared over several ticks


def dedupe_key(kind: str, user_id: int | None, payload: dict) -> str:
    """The one dedupe key: kind, user and payload (sorted), so equal requests land on one row."""
    return hashlib.sha256(f"{kind}|{user_id}|{json.dumps(payload, sort_keys=True, default=str)}".encode()).hexdigest()


async def add(
    db: AsyncSession,
    kind: str,
    payload: dict | None = None,
    *,
    user_id: int | None = None,
    run_after: datetime | None = None,
    dedupe: bool = False,
    rerun: bool = False,
) -> Task:
    """Queue a task in the caller's transaction (flushes, never commits). With dedupe=True, return the
    queued/running task of the same kind, user and payload instead of adding another. With rerun=True,
    dedupe only against a queued one: asked while the task runs (on older inputs), this queues exactly
    one follow-up, which `claim` holds back until the run ends. Concurrent callers converge on one
    queued row through the partial unique index."""
    payload, now = payload or {}, utcnow()
    values = {"kind": kind, "queue": queue_for(kind), "payload": payload, "user_id": user_id,
              "run_after": run_after or now, "status": "queued", "attempts": 0}
    if not (dedupe or rerun):
        task = Task(**values)
        db.add(task)
        await db.flush()
        _note(db, task, now)
        return task
    key = dedupe_key(kind, user_id, payload)
    twin = select(Task).where(Task.dedupe_key == key).order_by(Task.id.desc()).limit(1)
    insert = (
        pg_insert(Task).values(**values, dedupe_key=key)
        # A literal, not a bind: under a generic plan (asyncpg's prepared statement, after five runs) a
        # bound status can't match the partial index's predicate, and Postgres rejects the insert.
        .on_conflict_do_nothing(index_elements=["dedupe_key"],
                                index_where=Task.status == literal_column("'queued'"))
        .returning(Task)
    )
    for _ in range(2):  # the second pass covers the queued twin being claimed between our insert and select
        # A conflicting insert by an open transaction makes this wait for it, so the select below sees it.
        task = await db.scalar(select(Task).from_statement(insert))
        if task:
            # Checked after the insert, so a queued twin claimed meanwhile is seen running here.
            if not rerun and (running := await db.scalar(twin.where(Task.status == "running"))):
                await db.delete(task)
                await db.flush()
                return running
            _note(db, task, now)
            return task
        if queued := await db.scalar(twin.where(Task.status == "queued")):
            return queued
    raise RuntimeError("ledger dedupe could not settle")


async def claim(db: AsyncSession, task_id: int) -> Task | None:
    """queued -> running, atomically. None when the row isn't queued and due: another delivery already
    took it, it finished, or it was rescheduled (duplicates are normal; drop them).

    A rerun queued behind a running twin isn't claimed: its run_after moves TWIN_WAIT out for the relay
    to deliver it again (the caller commits that), unless the twin's finish wakes it first. Race-safe
    because the unique index allows one queued twin per key: the only race is this claim against the
    twin finishing, which at worst defers the rerun by one TWIN_WAIT."""
    now = utcnow()
    due = (Task.id == task_id, Task.status == "queued", Task.run_after <= now)
    claimed = await db.scalar(
        update(Task).where(*due, ~has_twin("running"))
        .values(status="running", locked_at=now, attempts=Task.attempts + 1).returning(Task.id)
    )
    if claimed:
        return await db.get(Task, claimed, populate_existing=True)
    # Still queued and due here means a running twin (or one that just finished): try again later.
    await db.execute(update(Task).where(*due).values(run_after=now + TWIN_WAIT, sent_at=None))
    return None


def has_twin(status: str):
    """EXISTS another `status` row with the same dedupe_key as the Task row in the statement."""
    other = aliased(Task)
    return exists().where(other.dedupe_key == Task.dedupe_key, other.id != Task.id, other.status == status)


async def finish(db: AsyncSession, task: Task, result: dict) -> None:
    task.status, task.result, task.error, task.finished_at = "done", result, None, utcnow()
    await db.flush()
    await _wake_rerun(db, task.dedupe_key)


async def fail(db: AsyncSession, task: Task, error: str, *, retry: bool) -> bool:
    """Record a failure; with retry, queue it again after a backoff (TASK_BACKOFF_SECONDS x4 per attempt:
    30s, 2m, 8m) until TASK_MAX_ATTEMPTS. The relay delivers the retry once it's due. A queued twin (a
    rerun) does the same work again, so it replaces the retry. Returns True when this is the end of it:
    failed, with no retry or rerun left to do the work (the cue for tasks.failures)."""
    key, attempts, now = task.dedupe_key, task.attempts, utcnow()
    task.error = error[:2000]
    replaced = bool(await db.scalar(select(Task.id).where(Task.id == task.id, has_twin("queued"))))
    if retry and attempts < settings.task_max_attempts and not replaced:
        try:
            async with db.begin_nested():  # a rerun added meanwhile conflicts with this on the unique index
                task.status, task.sent_at, task.queue = "queued", None, QUEUES.get(task.kind, task.queue)
                task.run_after = now + timedelta(seconds=settings.task_backoff_seconds * 4 ** (attempts - 1))
            return False
        except IntegrityError:
            replaced = True  # the savepoint's rollback expired `task`: only set attributes below, never read them
    task.status, task.error, task.finished_at = "failed", error[:2000], now
    await db.flush()
    await _wake_rerun(db, key)
    return not replaced


async def _wake_rerun(db: AsyncSession, key: str | None) -> None:
    """A rerun that claim deferred behind the task that just ended is due now: publish it after commit."""
    if key is None:
        return
    now = utcnow()
    rerun = await db.scalar(
        select(Task).from_statement(
            update(Task).where(Task.dedupe_key == key, Task.status == "queued", Task.run_after <= now + TWIN_WAIT)
            .values(run_after=now, sent_at=None).returning(Task)
        ).execution_options(populate_existing=True)
    )
    if rerun:
        _note(db, rerun, now)


async def heartbeat(db: AsyncSession, task_id: int) -> None:
    """Called periodically while a task runs so `reap` can tell it from one whose worker died."""
    await db.execute(update(Task).where(Task.id == task_id, Task.status == "running").values(locked_at=utcnow()))


UNCLAIMED = {"unclaimed": True}  # the result of a task expire_queued failed: no worker ever took it


async def expire_queued(db: AsyncSession, task_id: int, reason: str) -> bool:
    """Fail a task nobody claimed in time, so no backlog is left behind; True if it was still queued.
    Its result is UNCLAIMED, so whoever waits on it can tell that from the work failing."""
    result = await db.execute(
        update(Task).where(Task.id == task_id, Task.status == "queued")
        .values(status="failed", error=reason, result=UNCLAIMED, finished_at=utcnow())
    )
    return bool(result.rowcount)


async def active_keys(db: AsyncSession, keys: list[str]) -> set[str]:
    """The dedupe keys among `keys` (dedupe_key's) that have a queued or running task."""
    if not keys:
        return set()
    return set(await db.scalars(select(Task.dedupe_key).where(Task.dedupe_key.in_(keys), Task.status.in_(ACTIVE))))


async def requeue(db: AsyncSession, task_id: int) -> Task:
    """Run a finished or failed task again from scratch (the admin's retry). Raises Conflict while it's
    still active or an identical task is, NotFound when there's no such task."""
    task = await db.get(Task, task_id)
    if task is None:
        raise NotFound(f"No task {task_id}")
    if task.status in ACTIVE:
        raise Conflict(f"task {task_id} is already {task.status}")
    if task.kind not in QUEUES:
        raise Conflict(f"{task.kind} is no longer a task kind")
    twin = Conflict(f"an identical {task.kind} task is already queued or running")
    if task.dedupe_key and await db.scalar(  # the common case, without a savepoint
        select(Task.id).where(Task.dedupe_key == task.dedupe_key, Task.status.in_(ACTIVE))
    ):
        raise twin
    now = utcnow()
    await db.flush()  # the caller's own pending writes, outside the savepoint below
    try:
        async with db.begin_nested():  # a twin added since the check rolls back only this
            task.status, task.attempts, task.error, task.result = "queued", 0, None, None
            task.queue = queue_for(task.kind)  # the kind may have moved queues since it first ran
            task.run_after, task.finished_at, task.locked_at, task.sent_at = now, None, None, None
    except IntegrityError as e:
        if getattr(e.orig.__cause__, "constraint_name", None) == DEDUPE_INDEX:  # asyncpg's error
            raise twin from None
        raise
    _note(db, task, now)
    return task


async def reap(db: AsyncSession, *, on_failed: Callable[[AsyncSession, list[Task]], Awaitable[None]] | None = None
               ) -> int:
    """Recover tasks whose worker died mid-run (no heartbeat for TASK_STUCK_AFTER_MINUTES): queue them
    again for the relay, or fail them once they used all their attempts. Returns the number requeued.
    `on_failed` gets the ones failed for good (no queued rerun takes over), like `fail`'s True."""
    attempts = settings.task_max_attempts
    stale = (Task.status == "running", Task.locked_at < utcnow() - timedelta(minutes=settings.task_stuck_after_minutes))
    failed = (await db.scalars(
        select(Task).from_statement(
            update(Task).where(*stale, Task.attempts >= attempts, ~has_twin("queued"))
            .values(status="failed", finished_at=utcnow(), error=f"worker stopped responding during {attempts} attempts")
            .returning(Task)
        ).execution_options(populate_existing=True)
    )).all()
    # A stale row whose rerun is already queued leaves the work to it (the index allows one queued twin).
    superseded = await db.execute(
        update(Task).where(*stale, has_twin("queued"))
        .values(status="failed", finished_at=utcnow(), error="worker stopped responding; a queued rerun replaces it")
    )
    try:
        async with db.begin_nested():  # a rerun added meanwhile conflicts on the unique index: next reap
            requeued = await db.execute(
                update(Task).where(*stale, ~has_twin("queued"))
                .values(status="queued", sent_at=None, queue=case(QUEUES, value=Task.kind, else_=Task.queue))
            )
    except IntegrityError:
        log.info("a rerun was queued while reaping; the next reap recovers the rest")
        requeued = None
    if superseded.rowcount:
        log.info("failed %d stale task(s) that a queued rerun replaces", superseded.rowcount)
    if failed:
        log.warning("failed %d task(s) that kept timing out", len(failed))
        if on_failed is not None:
            await on_failed(db, list(failed))
    return requeued.rowcount if requeued else 0


async def prune(db: AsyncSession, *, batch: int = PRUNE_BATCH) -> int:
    """Delete up to `batch` done or failed rows that finished more than TASK_PRUNE_DAYS ago (#50).
    Queued and running rows are never touched. Returns the number deleted."""
    cutoff = utcnow() - timedelta(days=settings.task_prune_days)
    old = (
        select(Task.id).where(Task.status.in_(FINISHED), Task.finished_at < cutoff)
        .order_by(Task.finished_at).limit(batch)
    )
    result = await db.execute(delete(Task).where(Task.id.in_(old.scalar_subquery())))
    return result.rowcount


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


async def mark_sent(db: AsyncSession, ids: list[int], now: datetime, *, unclaimed_only: bool = False,
                    relayed: bool = False) -> None:
    """Stamp sent_at on published rows. unclaimed_only (the after-commit publish) only stamps rows still
    queued, unsent and never claimed, so a fast failure's retry (sent_at reset) isn't hidden from the relay.
    relayed (the relay) only stamps rows still as due_for_relay found them: queued, due at `now`, and unsent
    or sent before RELAY_RESEND. A row claimed meanwhile and queued again (a retry, or a rerun deferred
    behind its twin) is due later with sent_at NULL, and stays the relay's to send."""
    if ids:
        where = [Task.id.in_(ids)]
        if unclaimed_only:
            where += [Task.status == "queued", Task.sent_at.is_(None), Task.attempts == 0]
        if relayed:
            where += [Task.status == "queued", Task.run_after <= now,
                      or_(Task.sent_at.is_(None), Task.sent_at < now - RELAY_RESEND)]
        await db.execute(update(Task).where(*where).values(sent_at=now))


# ---------------------------------------------------------------- publish after commit

_publishing: set[asyncio.Task] = set()  # strong refs until each publish finishes


def _note(db: AsyncSession, task: Task, now: datetime) -> None:
    if task.run_after <= now:  # later rows wait for the relay
        db.info.setdefault(PENDING, []).append((task.queue, task.kind, task.id))


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    if session.in_nested_transaction():  # a savepoint released: the rows aren't committed yet
        return
    pending = session.info.pop(PENDING, None)
    if not pending:
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
    """Publish the committed rows, then mark them sent so the relay leaves them alone. A row whose
    publish failed keeps sent_at NULL, and the relay sends it once RELAY_GRACE has passed."""
    from jobfinder.tasks import brokers

    sent: list[int] = []
    failed: set[str] = set()  # a queue that failed once: its other rows wait for the relay too
    for queue, kind, task_id in pending:
        if queue in failed:
            continue
        try:
            await brokers.publish(queue, kind, task_id)
        except Exception as e:  # valkey down: the row stays queued and the relay re-sends it
            failed.add(queue)
            log.warning("publishing task %s (%s) failed, the relay will retry: %s: %s",
                        task_id, kind, type(e).__name__, e)
            continue
        sent.append(task_id)
    if sent:
        await _mark_published(sent)


async def _mark_published(ids: list[int]) -> None:
    """One UPDATE in its own short session. Failing only costs a duplicate delivery from the relay."""
    try:
        async with default_context().sessions() as db:
            await mark_sent(db, ids, utcnow(), unclaimed_only=True)
            await db.commit()
    except Exception as e:
        log.warning("marking %d published task(s) sent failed: %s: %s", len(ids), type(e).__name__, e)
