"""What a task that failed for good leaves behind: the item it was meant to finish.

A document or link read by parse_document / fetch_link stays "processing", and a pasted job link stays
"resolving", until its task records the outcome; the SPA polls until then. When the task fails instead
(out of attempts, an unexpected error, or the reaper giving up on a dead worker) and no retry or rerun is
left to do the work, `settle` finishes the item as an error, so nothing waits on it any longer. Called by
the runtime (tasks.runtime._fail) and by the scheduler's maintenance (ledger.reap's on_failed), in their
transaction; each hook runs in a savepoint, and one that fails is logged and skipped.
"""
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import Task

log = logging.getLogger("jobfinder.tasks")


async def _document(db: AsyncSession, task: Task) -> None:
    from jobfinder.context import default_context
    from jobfinder.services import documents

    if task.user_id is not None and (doc_id := (task.payload or {}).get("doc_id")):
        documents.unfinished(default_context().store, task.user_id, str(doc_id))


async def _job_link(db: AsyncSession, task: Task) -> None:
    from jobfinder.services.resolver import give_up

    if isinstance(link_id := (task.payload or {}).get("link_id"), int):
        await give_up(db, link_id)


HOOKS: dict[str, Callable[[AsyncSession, Task], Awaitable[None]]] = {
    "parse_document": _document,
    "fetch_link": _document,
    "resolve_job_link": _job_link,
    "induce_recipe": _job_link,
}


async def settle(db: AsyncSession, tasks: list[Task]) -> None:
    """Run each failed task's hook, if its kind has one. Never raises."""
    for task in tasks:
        if (hook := HOOKS.get(task.kind)) is None:
            continue
        try:
            async with db.begin_nested():
                await hook(db, task)
        except Exception:
            log.exception("settling failed task %s (%s) failed", task.id, task.kind)
