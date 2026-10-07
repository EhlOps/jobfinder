from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.auth import ratelimit
from jobfinder.auth.security import current_user
from jobfinder.config import get_settings
from jobfinder.db import get_db
from jobfinder.models import User
from jobfinder.notify import daily
from jobfinder.notify.email import EmailError

router = APIRouter(prefix="/api/settings", tags=["settings"])
DB = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(current_user)]


class SettingsBody(BaseModel):
    digest_enabled: bool
    digest_hour: int = Field(ge=0, le=23)
    timezone: str = Field(min_length=1, max_length=64)
    question_emails_enabled: bool

    @field_validator("timezone")
    @classmethod
    def _valid_zone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown timezone") from None
        return v


def _out(u: User) -> SettingsBody:
    return SettingsBody(
        digest_enabled=bool(u.digest_enabled), digest_hour=8 if u.digest_hour is None else u.digest_hour,
        timezone=u.timezone or "UTC", question_emails_enabled=bool(u.question_emails_enabled),
    )


@router.get("", response_model=SettingsBody)
async def read_settings(user: CurrentUser):
    return _out(user)


@router.put("", response_model=SettingsBody)
async def write_settings(body: SettingsBody, user: CurrentUser, db: DB):
    user.digest_enabled, user.digest_hour = body.digest_enabled, body.digest_hour
    user.timezone, user.question_emails_enabled = body.timezone, body.question_emails_enabled
    await db.commit()
    return _out(user)


class PreviewResult(BaseModel):
    to: str
    matches: int
    questions: int


@router.post("/digest/preview", response_model=PreviewResult)
async def send_preview(user: CurrentUser, db: DB):
    """Email yourself what the daily email looks like right now (marks nothing as sent)."""
    s = get_settings()
    if await ratelimit.count_since(db, "preview_email", str(user.id), 3600) >= s.preview_emails_per_hour:
        raise HTTPException(429, "Too many preview emails. Try again later.")
    await ratelimit.hit(db, "preview_email", str(user.id))
    try:
        result = await daily.send_daily(db, user, preview=True)
    except EmailError as e:
        raise HTTPException(502, f"Couldn't send the email: {e}") from None
    if result is None:
        raise HTTPException(409, "Nothing to put in an email yet: no matches scoring 50+ or open questions.")
    return PreviewResult(to=user.email, matches=result["matches"], questions=result["questions"])
