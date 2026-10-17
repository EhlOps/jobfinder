"""@ledger_task: the worker side of a task kind.

On delivery of a ledger id: claim the row (a duplicate delivery finds it already taken and is dropped)
and commit, so the SPA sees "running" and the reaper has a heartbeat to watch; validate the payload; run
the handler in the same session; then finish and commit. taskiq acks after this returns, whatever the
outcome. A worker that dies mid-run never acks: its row stays `running` until `ledger.reap` queues it
again, and the stream's redelivery then fails the claim and is dropped.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from taskiq import AsyncBroker

from jobfinder import db as dbmod
from jobfinder.core.config import settings
from jobfinder.integrations.claude.claude_code import AIError
from jobfinder.models import Task
from jobfinder.tasks import ledger
from jobfinder.tasks.kinds import queue_for

log = logging.getLogger("jobfinder.tasks")
# handler(db, ai, task, payload): ai is None for kinds registered with ai=False (the renderer has no
# Claude); payload is the validated model, or None for kinds without one.
LedgerHandler = Callable[[AsyncSession, Any, Task, Any], Awaitable[dict]]
REGISTRY: dict[str, LedgerHandler] = {}


def ledger_task(broker: AsyncBroker, kind: str, *, payload: type[BaseModel] | None = None, ai: bool = True):
    """Register `handler` for `kind` on `broker` (which must be its queue's broker, tasks.kinds)."""
    queue_for(kind)  # an unknown kind fails at import, not at first delivery

    def register(handler: LedgerHandler) -> LedgerHandler:
        async def deliver(task_id: int) -> None:
            await execute(kind, handler, int(task_id), payload_model=payload, wants_ai=ai)

        broker.register_task(deliver, task_name=kind)
        REGISTRY[kind] = handler
        return handler

    return register


async def execute(kind: str, handler: LedgerHandler, task_id: int, *, payload_model: type[BaseModel] | None = None,
                  wants_ai: bool = True, session_factory=None, ai=None) -> None:
    sessions = session_factory or dbmod.SessionLocal
    async with sessions() as db:
        task = await ledger.claim(db, task_id)
        if task is None:
            log.debug("task %s (%s) not claimable; duplicate delivery dropped", task_id, kind)
            return
        await db.commit()
        beat = asyncio.create_task(_heartbeat_loop(task_id, sessions))
        try:
            if task.kind != kind:
                raise ValueError(f"task {task_id} is a {task.kind!r}, delivered as {kind!r}")
            args = payload_model.model_validate(task.payload or {}) if payload_model else None
            if wants_ai and ai is None:
                from jobfinder.context import default_context

                ai = default_context().claude  # records every call in ai_calls
            result = await handler(db, ai if wants_ai else None, task, args)
            await ledger.finish(db, task, result)
            await db.commit()
        except AIError as e:
            log.warning("task %s (%s) AI error: %s", task_id, kind, e)
            await db.rollback()
            await _fail(sessions, task_id, str(e), retry=e.retryable)
        except Exception as e:
            log.exception("task %s (%s) failed", task_id, kind)
            await db.rollback()
            await _fail(sessions, task_id, f"{type(e).__name__}: {e}", retry=False)
        finally:
            beat.cancel()


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
    """Record a failure on a fresh session: the handler's may be in a failed transaction."""
    try:
        async with sessions() as db:
            task = await db.get(Task, task_id)
            if task:
                await ledger.fail(db, task, error, retry=retry)
                await db.commit()
    except Exception:
        log.exception("could not record failure of task %s; the reaper will recover it", task_id)
