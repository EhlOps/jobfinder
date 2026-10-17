"""The admin's view of the task ledger (`jobfinder tasks list|retry`): every user's tasks, not one's own.

Retry is ledger.requeue: it flushes, and the command commits (the ledger publishes the row after that).
"""
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import Task
from jobfinder.repositories import tasks
from jobfinder.tasks import ledger


async def recent(
    db: AsyncSession, *, status: str | None = None, kind: str | None = None, limit: int = 50
) -> list[Task]:
    return await tasks.recent(db, status=status, kind=kind, limit=limit)


async def retry(db: AsyncSession, task_id: int) -> Task:
    """Queue a finished or failed task again (NotFound, or Conflict while it or a twin is active)."""
    return await ledger.requeue(db, task_id)
