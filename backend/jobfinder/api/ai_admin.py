"""Connect Claude from the web app (the admin pastes the token from `claude setup-token`), and the admin's
views of the planner schedule and board health."""
from fastapi import APIRouter

from jobfinder.api.deps import AdminUser, CurrentUser, DbSession
from jobfinder.schemas.ai_admin import AiStatus, Schedule, SourceRow, TokenIn
from jobfinder.schemas.common import TaskRef
from jobfinder.services import admin

router = APIRouter(tags=["ai"])


@router.get("/api/ai/status", response_model=AiStatus)
async def ai_status(_: CurrentUser, db: DbSession):
    """Any signed-in user may see whether the AI is connected (never the token itself)."""
    return await admin.ai_status(db)


@router.put("/api/admin/ai/token", response_model=TaskRef, status_code=202)
async def set_token(body: TokenIn, user: AdminUser, db: DbSession):
    return TaskRef(task_id=await admin.set_token(db, user, body.token))  # verified right away


@router.post("/api/admin/ai/check", response_model=TaskRef, status_code=202)
async def check(user: AdminUser, db: DbSession):
    return TaskRef(task_id=await admin.check_ai(db, user))


@router.delete("/api/admin/ai/token", status_code=204)
async def delete_token(_: AdminUser):
    admin.delete_token()


@router.get("/api/admin/schedule", response_model=Schedule)
async def schedule(_: AdminUser, db: DbSession):
    """What the background planner has done and will do for each user."""
    return await admin.schedule(db)


@router.get("/api/admin/sources", response_model=list[SourceRow])
async def sources(_: AdminUser, db: DbSession):
    """Per-board ingest health, failing boards first."""
    return await admin.sources(db)


@router.post("/api/admin/sources/{company_id}/enable", response_model=SourceRow)
async def enable_source(company_id: int, _: AdminUser, db: DbSession):
    """Re-enable a disabled board; counters reset so one more failure does not disable it again."""
    return await admin.enable(db, company_id)
