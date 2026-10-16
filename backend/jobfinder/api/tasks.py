from fastapi import APIRouter, HTTPException

from jobfinder.api.deps import CurrentUser, DbSession
from jobfinder.models import Task
from jobfinder.schemas.tasks import TaskOut

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: int,
    user: CurrentUser,
    db: DbSession,
):
    task = await db.get(Task, task_id)
    if not task or task.user_id != user.id:
        raise HTTPException(404, "Not found")
    return task
