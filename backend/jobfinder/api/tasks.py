from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jobfinder.api.deps import CurrentUser, DbSession
from jobfinder.models import Task

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskOut(BaseModel):
    id: int
    kind: str
    status: str
    result: dict[str, Any] | None
    error: str | None

    model_config = {"from_attributes": True}


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
