from fastapi import APIRouter

from jobfinder.api.deps import Ctx, CurrentUser, DbSession
from jobfinder.schemas.settings import PreviewResult, SettingsBody
from jobfinder.services import settings as user_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("", response_model=SettingsBody)
async def read_settings(user: CurrentUser):
    return user_settings.read(user)


@router.put("", response_model=SettingsBody)
async def write_settings(body: SettingsBody, user: CurrentUser, db: DbSession):
    return await user_settings.write(db, user, body)


@router.post("/digest/preview", response_model=PreviewResult)
async def send_preview(user: CurrentUser, db: DbSession, ctx: Ctx):
    """Email yourself what the daily email looks like right now (marks nothing as sent)."""
    return await user_settings.send_preview(db, ctx.sessions, user)
