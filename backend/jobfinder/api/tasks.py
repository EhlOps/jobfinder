from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.auth.security import current_user
from jobfinder.db import get_db
from jobfinder.models import Task, User

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
    user: Annotated[User, Depends(current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    task = await db.get(Task, task_id)
    if not task or task.user_id != user.id:
        raise HTTPException(404, "Not found")
    return task
