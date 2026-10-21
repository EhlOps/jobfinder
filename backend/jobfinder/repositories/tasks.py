"""Task-ledger reads: the newest unfinished task of a kind for a match (API), recent tasks (CLI)."""
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import Task


async def pending_for(db: AsyncSession, user_id: int, kind: str, match_id: int) -> int | None:
    """The id of the newest queued or running `kind` task for this match, if any."""
    return (
        await db.execute(
            sa.select(Task.id)
            .where(
                Task.kind == kind, Task.user_id == user_id, Task.status.in_(("queued", "running")),
                Task.payload["match_id"].as_integer() == match_id,
            )
            .order_by(Task.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def owned(db: AsyncSession, user_id: int, task_id: int) -> Task | None:
    """The task, if it is this user's."""
    task = await db.get(Task, task_id)
    return task if task and task.user_id == user_id else None


async def recent(db: AsyncSession, *, status: str | None = None, kind: str | None = None, limit: int = 50) -> list[Task]:
    """The newest tasks first, optionally of one status or kind."""
    q = sa.select(Task).order_by(Task.id.desc()).limit(limit)
    if status:
        q = q.where(Task.status == status)
    if kind:
        q = q.where(Task.kind == kind)
    return list((await db.scalars(q)).all())


async def any_since(db: AsyncSession, user_id: int, kind: str, since: datetime) -> bool:
    """Whether the user has a task of `kind` due (run_after) at or after `since`, in any state."""
    return bool(await db.scalar(sa.select(sa.exists().where(
        Task.user_id == user_id, Task.kind == kind, Task.run_after >= since,
    ))))
