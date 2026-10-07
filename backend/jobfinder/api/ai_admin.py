"""Connect Claude from the web app: the admin pastes the token from `claude setup-token`."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.ai import credentials
from jobfinder.auth.security import current_user, require_admin
from jobfinder.db import get_db
from jobfinder.models import AICall, User
from jobfinder.scheduling import queue

router = APIRouter(tags=["ai"])

DB = Annotated[AsyncSession, Depends(get_db)]
Admin = Annotated[User, Depends(require_admin)]


class LastCall(BaseModel):
    ok: bool
    error_kind: str | None
    at: str


class AiStatus(BaseModel):
    configured: bool
    source: Literal["file", "env", "none"]
    last_call: LastCall | None


class TokenIn(BaseModel):
    token: str


class TaskRef(BaseModel):
    task_id: int


@router.get("/api/ai/status", response_model=AiStatus)
async def ai_status(_: Annotated[User, Depends(current_user)], db: DB):
    """Any signed-in user may see whether the AI is connected (never the token itself)."""
    src = credentials.source()
    row = (await db.execute(select(AICall).order_by(AICall.id.desc()).limit(1))).scalar_one_or_none()
    last = LastCall(ok=row.ok, error_kind=row.error_kind, at=row.created_at.isoformat()) if row else None
    return AiStatus(configured=src != "none", source=src, last_call=last)


@router.put("/api/admin/ai/token", response_model=TaskRef, status_code=202)
async def set_token(body: TokenIn, admin: Admin, db: DB):
    try:
        credentials.write_token(body.token)
    except credentials.InvalidToken as e:
        raise HTTPException(422, str(e)) from None
    task = await queue.enqueue(db, "ai_check", user_id=admin.id)  # verify it right away
    return TaskRef(task_id=task.id)


@router.post("/api/admin/ai/check", response_model=TaskRef, status_code=202)
async def check(admin: Admin, db: DB):
    task = await queue.enqueue(db, "ai_check", user_id=admin.id, dedupe=True)
    return TaskRef(task_id=task.id)


@router.delete("/api/admin/ai/token", status_code=204)
async def delete_token(_: Admin):
    credentials.delete_token()
