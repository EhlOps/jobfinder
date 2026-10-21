"""Reading the user's own background tasks (the SPA polls these)."""

from jobfinder.core.db import AsyncSession
from jobfinder.core.errors import NotFound
from jobfinder.models import Task
from jobfinder.repositories import tasks


async def get(db: AsyncSession, user_id: int, task_id: int) -> Task:
    """The task, or 404 when it doesn't exist or is someone else's (the two look the same)."""
    task = await tasks.owned(db, user_id, task_id)
    if not task:
        raise NotFound("Not found")
    return task
