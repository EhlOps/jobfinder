from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.auth import ratelimit
from jobfinder.auth.security import current_user
from jobfinder.config import get_settings
from jobfinder.db import get_db
from jobfinder.models import Profile, User
from jobfinder.notify import daily
from jobfinder.notify.email import EmailError
from jobfinder.scheduling import queue

router = APIRouter(prefix="/api/settings", tags=["settings"])
DB = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(current_user)]


class SettingsBody(BaseModel):
    digest_enabled: bool
    digest_hour: int = Field(ge=0, le=23)
    timezone: str = Field(min_length=1, max_length=64)
    question_emails_enabled: bool
    match_budget_enabled: bool = True
    match_budget: int | None = Field(default=None, ge=1, le=1000)  # None = keep the current value

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
        match_budget_enabled=u.match_budget_enabled is not False,
        match_budget=u.match_budget if u.match_budget is not None else get_settings().match_daily_llm_budget,
    )


@router.get("", response_model=SettingsBody)
async def read_settings(user: CurrentUser):
    return _out(user)


@router.put("", response_model=SettingsBody)
async def write_settings(body: SettingsBody, user: CurrentUser, db: DB):
    user.digest_enabled, user.digest_hour = body.digest_enabled, body.digest_hour
    user.timezone, user.question_emails_enabled = body.timezone, body.question_emails_enabled
    before = _out(user)
    user.match_budget_enabled = body.match_budget_enabled
    if body.match_budget is not None:
        user.match_budget = body.match_budget
    after = _out(user)
    # More room than before: start scoring the waiting jobs now rather than at the next scheduled run.
    roomier = after.match_budget_enabled and (
        not before.match_budget_enabled or after.match_budget > before.match_budget
    )
    profile = await db.get(Profile, user.id) if roomier else None
    if profile and profile.status:
        await queue.enqueue(db, "match_user", user_id=user.id, dedupe=True)
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
