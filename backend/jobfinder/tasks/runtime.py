"""@ledger_task: the worker side of a task kind.

`@ledger_task(kind)` registers its handler on the broker of the kind's queue, looked up from
tasks.kinds.QUEUES, so which stream delivers a kind is set in that one place. Importing a handler module
registers its kinds; a worker process only consumes its own queue's broker (tasks/__main__.py).

On delivery of a ledger id: claim the row (a duplicate delivery finds it already taken and is dropped)
and commit, so the SPA sees "running" and the reaper has a heartbeat to watch; validate the payload; run
the handler in the same session; then finish and commit. The handler's own `ledger.add` follow-ups are
published once that commit lands.

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
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.core.config import settings
from jobfinder.core.context import default_context
from jobfinder.integrations.claude.claude_code import AIError
from jobfinder.models import Task
from jobfinder.tasks import failures, ledger
from jobfinder.tasks.brokers import BROKERS
from jobfinder.tasks.kinds import queue_for

log = logging.getLogger("jobfinder.tasks")
# handler(db, ai, task, payload): ai is None for kinds registered with ai=False (no Claude: the io queue
# and the renderer); payload is the validated model, or None for kinds without one.
LedgerHandler = Callable[[AsyncSession, Any, Task, Any], Awaitable[dict]]
REGISTRY: dict[str, LedgerHandler] = {}
PAYLOADS: dict[str, type[BaseModel] | None] = {}  # kind -> its payload model


def ledger_task(kind: str, *, payload: type[BaseModel] | None = None, ai: bool = True):
    """Register `handler` for `kind` on its queue's broker (tasks.kinds.QUEUES)."""
    broker = BROKERS[queue_for(kind)]  # an unknown kind fails at import, not at first delivery

    def register(handler: LedgerHandler) -> LedgerHandler:
        async def deliver(task_id: int) -> None:
            await execute(kind, handler, int(task_id), payload_model=payload, wants_ai=ai)

        broker.register_task(deliver, task_name=kind)
        REGISTRY[kind], PAYLOADS[kind] = handler, payload
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


async def _execute(kind, handler, task_id, payload_model, wants_ai, sessions, ai) -> None:
    async with sessions() as db:
        task = await ledger.claim(db, task_id)
        if task is None:
            await db.commit()  # keeps claim's deferral of a rerun behind a running twin
            log.debug("task %s (%s) not claimable; duplicate delivery dropped", task_id, kind)
            return
        await db.commit()
        beat = asyncio.create_task(_heartbeat_loop(task_id, sessions))
        try:
            if task.kind != kind:
                raise ValueError(f"task {task_id} is a {task.kind!r}, delivered as {kind!r}")
            args = parse_payload(payload_model, task.payload)
            if wants_ai and ai is None:
                from jobfinder.context import default_context

                ai = default_context().claude  # built once per process (cached); records every call in ai_calls
            result = await handler(db, ai if wants_ai else None, task, args)
            await ledger.finish(db, task, result)
            await db.commit()
        except AIError as e:
            log.warning("task %s (%s) AI error: %s", task_id, kind, e)
            await _rollback(db, task_id)
            await _fail(sessions, task_id, str(e), retry=e.retryable)
        except Exception as e:
            log.exception("task %s (%s) failed", task_id, kind)
            await _rollback(db, task_id)
            await _fail(sessions, task_id, f"{type(e).__name__}: {e}", retry=False)
        finally:
            beat.cancel()


def parse_payload(model: type[BaseModel] | None, raw: dict | None) -> BaseModel | None:
    """A row's payload as its kind's model (None for kinds without one). Unknown keys are ignored."""
    return model.model_validate(raw or {}) if model else None


async def _rollback(db: AsyncSession, task_id: int) -> None:
    """Undo the handler's work. A dead connection fails the rollback too; the failure is still recorded
    on a fresh session (_fail), so the row doesn't sit `running` until the reaper."""
    try:
        await db.rollback()
    except Exception:
        log.warning("rollback after task %s failed (connection lost?)", task_id, exc_info=True)


async def _heartbeat_loop(task_id: int, sessions) -> None:
    while True:
        await asyncio.sleep(settings.task_heartbeat_seconds)
        try:
            async with sessions() as db:
                await ledger.heartbeat(db, task_id)
                await db.commit()
        except Exception:
            log.exception("heartbeat for task %s failed", task_id)


async def _fail(sessions, task_id: int, error: str, *, retry: bool) -> None:
    """Record a failure on a fresh session: the handler's may be in a failed transaction. A task that
    failed for good settles what it leaves behind (tasks.failures) in the same commit."""
    try:
        async with sessions() as db:
            task = await db.get(Task, task_id)
            if task:
                if await ledger.fail(db, task, error, retry=retry):
                    await failures.settle(db, [task])
                await db.commit()
    except Exception:
        log.exception("could not record failure of task %s; the reaper will recover it", task_id)
