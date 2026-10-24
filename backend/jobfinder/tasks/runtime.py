"""@ledger_task: the worker side of a task kind.

`@ledger_task(kind)` registers its handler on the broker of the kind's queue, looked up from
tasks.kinds.QUEUES, so which stream delivers a kind is set in that one place. Importing a handler module
registers its kinds; a worker process only consumes its own queue's broker (tasks/__main__.py).

On delivery of a ledger id: claim the row (a duplicate delivery finds it already taken and is dropped)
and commit, so the SPA sees "running" and the reaper has a heartbeat to watch; validate the payload; run
the handler in the same session; then finish and commit. The handler's own `ledger.add` follow-ups are
published once that commit lands. Every claimed run logs one outcome line at INFO (done, will retry, failed).

Every run ends by its kind's deadline (#136): `@ledger_task(kind, deadline=...)`, else TASK_DEADLINE_MINUTES.
A run still going then has its await stack logged, is cancelled, and is recorded as a retryable failure
(`timeout: ...`), so a hung handler can't hold its row `running` (and every rerun deduped onto it) forever.
The heartbeat stops HEARTBEAT_GRACE after the deadline in any case, so a delivery that can't even record
that outcome goes stale and the reaper requeues the row.

taskiq acks the message after `deliver` returns (--ack-type when_executed), and `deliver` always
returns: every outcome, failures included, is recorded on the row, and an error the runtime can't even
record (Postgres down) is logged once here instead of again by taskiq. The ledger, not the stream, is
what gets the work done, so an ack never loses anything:

  - A worker that dies mid-run never acks. Its row stays `running` until `ledger.reap` sees the stale
    heartbeat and queues it again; the relay re-sends it, and the stream's own redelivery of the old
    message (taskiq-redis XAUTOCLAIMs messages idle for 10 minutes) fails the claim and is dropped.
  - A delivery for a row that is still `running` (a slow task redelivered, or a dead worker's row before
    the reaper got to it) fails the claim and is dropped too. The reaper is the only way back.
  - On SIGTERM taskiq stops reading new messages and waits for the running ones (with no
    --wait-tasks-timeout it waits until Kubernetes' grace period ends in SIGKILL); a task cut off there
    is a dead worker's task as above. Messages fetched but not started stay unacked in the stream and
    their rows stay queued, so the relay re-sends them.
"""
import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.core.config import settings
from jobfinder.core.context import default_context
from jobfinder.integrations.claude.claude_code import AIError
from jobfinder.models import Task
from jobfinder.tasks import diagnostics, failures, ledger
from jobfinder.tasks.brokers import BROKERS
from jobfinder.tasks.kinds import queue_for

log = logging.getLogger("jobfinder.tasks")
# handler(db, ai, task, payload): ai is None for kinds registered with ai=False (no Claude: the io queue
# and the renderer); payload is the validated model, or None for kinds without one.
LedgerHandler = Callable[[AsyncSession, Any, Task, Any], Awaitable[dict]]
REGISTRY: dict[str, LedgerHandler] = {}
PAYLOADS: dict[str, type[BaseModel] | None] = {}  # kind -> its payload model
DEADLINES: dict[str, timedelta | None] = {}  # kind -> its own deadline; None: TASK_DEADLINE_MINUTES
HEARTBEAT_GRACE = timedelta(minutes=2)  # beats stop this long after the deadline (#136)
CANCEL_WAIT_SECONDS = 5  # how long a run past its deadline gets to unwind after its cancellation


class DeadlineExceeded(Exception):
    """A run went past its kind's deadline and was cancelled (#136)."""


def ledger_task(kind: str, *, payload: type[BaseModel] | None = None, ai: bool = True,
                deadline: timedelta | None = None):
    """Register `handler` for `kind` on its queue's broker (tasks.kinds.QUEUES). A run of it is cancelled
    and retried after `deadline` (None: TASK_DEADLINE_MINUTES, read at run time)."""
    broker = BROKERS[queue_for(kind)]  # an unknown kind fails at import, not at first delivery

    def register(handler: LedgerHandler) -> LedgerHandler:
        async def deliver(task_id: int) -> None:
            await execute(kind, handler, int(task_id), payload_model=payload, wants_ai=ai)

        broker.register_task(deliver, task_name=kind)
        REGISTRY[kind], PAYLOADS[kind], DEADLINES[kind] = handler, payload, deadline
        return handler

    return register


async def execute(kind: str, handler: LedgerHandler, task_id: int, *, payload_model: type[BaseModel] | None = None,
                  wants_ai: bool = True, session_factory=None, ai=None) -> None:
    """Run one delivery of `task_id` to its end. Never raises (but lets a cancellation through)."""
    sessions = session_factory or default_context().sessions
    try:
        await _execute(kind, handler, task_id, payload_model, wants_ai, sessions, ai)
    except Exception:  # the claim or the failure record itself failed: the row is queued or running
        log.exception("task %s (%s): delivery failed before its outcome was recorded; "
                      "the relay or the reaper will recover it", task_id, kind)


def deadline_for(kind: str) -> timedelta:
    """How long a run of `kind` may take before it is cancelled and retried (#136)."""
    own = DEADLINES.get(kind)
    return own if own is not None else timedelta(minutes=settings.task_deadline_minutes)


async def _execute(kind, handler, task_id, payload_model, wants_ai, sessions, ai) -> None:
    async with sessions() as db:
        task = await ledger.claim(db, task_id)
        if task is None:
            await db.commit()  # keeps claim's deferral of a rerun behind a running twin
            log.debug("task %s (%s) not claimable; duplicate delivery dropped", task_id, kind)
            return
        attempt = task.attempts  # finish and fail write only while the row is still this attempt (#136)
        _warn_if_slow(task)
        await db.commit()
        deadline, started = deadline_for(kind), time.monotonic()
        until = started + (deadline + HEARTBEAT_GRACE).total_seconds()
        beat = asyncio.create_task(_heartbeat_loop(task_id, sessions, attempt=attempt, until=until))
        try:
            if task.kind != kind:
                raise ValueError(f"task {task_id} is a {task.kind!r}, delivered as {kind!r}")
            args = parse_payload(payload_model, task.payload)
            if wants_ai and ai is None:
                from jobfinder.context import default_context

                ai = default_context().claude  # built once per process (cached); records every call in ai_calls
            result = await _run_by_deadline(handler(db, ai if wants_ai else None, task, args), deadline,
                                            task_id, kind)
            if await ledger.finish(db, task, result, attempt=attempt):
                await db.commit()
                log.info("task %s (%s) done in %.1fs", task_id, kind, time.monotonic() - started)
            else:  # the reaper took the row back: this run's writes and follow-ups are not ours to keep
                await _rollback(db, task_id)
                log.info("task %s (%s) done after %.1fs, not recorded: its row was taken back",
                         task_id, kind, time.monotonic() - started)
        except DeadlineExceeded:
            await _rollback(db, task_id)
            error = f"timeout: task exceeded its {deadline} deadline"
            _log_outcome(task_id, kind, started, error,
                         await _fail(sessions, task_id, error, retry=True, attempt=attempt))
        except AIError as e:
            log.warning("task %s (%s) AI error: %s", task_id, kind, e)
            await _rollback(db, task_id)
            _log_outcome(task_id, kind, started, str(e),
                         await _fail(sessions, task_id, str(e), retry=e.retryable, attempt=attempt))
        except Exception as e:
            log.exception("task %s (%s) failed", task_id, kind)
            await _rollback(db, task_id)
            error = f"{type(e).__name__}: {e}"
            _log_outcome(task_id, kind, started, error, await _fail(sessions, task_id, error, retry=False, attempt=attempt))
        finally:
            beat.cancel()


async def _run_by_deadline(handler_run: Awaitable[dict], deadline: timedelta, task_id: int, kind: str) -> dict:
    """The handler's result, or DeadlineExceeded once it has run for `deadline` (#136). The handler runs as
    its own asyncio task so it can be cancelled, with its await stack logged first: that stack is where
    the run hung. A cancellation of the delivery itself (worker shutdown) cancels the handler too."""
    run = asyncio.ensure_future(handler_run)
    try:
        done, _ = await asyncio.wait({run}, timeout=deadline.total_seconds())
    except asyncio.CancelledError:
        run.cancel()
        await asyncio.wait({run}, timeout=CANCEL_WAIT_SECONDS)  # let it unwind before its session closes
        raise
    if done:
        return run.result()
    log.error("task %s (%s) passed its %s deadline; its stack:\n%s", task_id, kind, deadline, diagnostics.await_stack(run))
    run.cancel()
    await asyncio.wait({run}, timeout=CANCEL_WAIT_SECONDS)
    if not run.done():  # it swallowed the cancellation, or blocks in its cleanup: left behind
        log.warning("task %s (%s) did not unwind within %ss of its cancellation; recording the timeout anyway",
                    task_id, kind, CANCEL_WAIT_SECONDS)
    elif not run.cancelled():
        with contextlib.suppress(BaseException):
            run.exception()  # retrieved, so asyncio doesn't log "exception was never retrieved"
    raise DeadlineExceeded


def _log_outcome(task_id: int, kind: str, started: float, error: str, status: str | None) -> None:
    """The one INFO line of a run that failed: queued again (a retry), failed for good, or not recorded
    (the reaper took the row back, or the write failed and the reaper will; `_fail` and the ledger log why)."""
    took = time.monotonic() - started
    if status == "queued":
        log.info("task %s (%s) will retry, failed after %.1fs: %s", task_id, kind, took, error)
    elif status is None:
        log.info("task %s (%s) failed after %.1fs, not recorded: %s",
                 task_id, kind, took, error)
    else:
        log.info("task %s (%s) failed after %.1fs: %s", task_id, kind, took, error)


def parse_payload(model: type[BaseModel] | None, raw: dict | None) -> BaseModel | None:
    """A row's payload as its kind's model (None for kinds without one). Unknown keys are ignored."""
    return model.model_validate(raw or {}) if model else None


def _warn_if_slow(task: Task) -> None:
    """Log a claimed task that waited longer than ledger.SLOW_WAIT to be picked up (#124's PRD 1)."""
    wait = task.started_at - task.run_after
    if wait > ledger.SLOW_WAIT:
        log.warning("task %s (%s) on %s waited %.1fs before claim", task.id, task.kind, task.queue,
                    wait.total_seconds())


async def _rollback(db: AsyncSession, task_id: int) -> None:
    """Undo the handler's work. A dead connection fails the rollback too; the failure is still recorded
    on a fresh session (_fail), so the row doesn't sit `running` until the reaper."""
    try:
        await db.rollback()
    except Exception:
        log.warning("rollback after task %s failed (connection lost?)", task_id, exc_info=True)


async def _heartbeat_loop(task_id: int, sessions, *, attempt: int, until: float) -> None:
    """Refresh the row's lock every TASK_HEARTBEAT_SECONDS, but not past `until` (time.monotonic(): the
    deadline plus HEARTBEAT_GRACE). The delivery normally records the timeout itself and stops this loop;
    the cap is for a delivery that can't get that far (stuck in its rollback or the failure record), whose
    beats would otherwise keep the row `running`, out of the reaper's reach, for good (#136)."""
    while True:
        await asyncio.sleep(settings.task_heartbeat_seconds)
        if time.monotonic() >= until:
            log.warning("task %s: heartbeat stopped %s past its deadline; the reaper will requeue it",
                        task_id, HEARTBEAT_GRACE)
            return
        try:
            async with sessions() as db:
                await ledger.heartbeat(db, task_id, attempt=attempt)
                await db.commit()
        except Exception:
            log.exception("heartbeat for task %s failed", task_id)


async def _fail(sessions, task_id: int, error: str, *, retry: bool, attempt: int) -> str | None:
    """Record a failure on a fresh session: the handler's may be in a failed transaction. A task that
    failed for good settles what it leaves behind (tasks.failures) in the same commit. Returns the row's
    new status (`queued`: a retry), or None when nothing was recorded: the write failed, or the reaper
    took the row back from this attempt (#136; ledger.fail checks that too, and logs it)."""
    try:
        async with sessions() as db:
            # Locked, so a reap can't slip in between this check and fail's own (the same check, under it).
            task = await db.get(Task, task_id, with_for_update=True)
            if task:
                ours = (task.status, task.attempts) == ("running", attempt)
                ended = await ledger.fail(db, task, error, retry=retry, attempt=attempt)
                status = task.status if ours else None  # set by fail; read before anything expires it
                if ended:
                    await failures.settle(db, [task])
                await db.commit()
                return status
    except Exception:
        log.exception("could not record failure of task %s; the reaper will recover it", task_id)
    return None
