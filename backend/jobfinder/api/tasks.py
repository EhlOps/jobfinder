from fastapi import APIRouter

from jobfinder.api.deps import CurrentUser, DbSession
from jobfinder.schemas.tasks import TaskOut
from jobfinder.services import tasks

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: int,
    user: CurrentUser,
    db: DbSession,
):
    return await tasks.get(db, user.id, task_id)
