import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.auth import ratelimit
from jobfinder.auth.security import (
    COOKIE_NAME,
    create_session,
    current_user,
    hash_password_async,
    hash_session_token,
    is_admin_email,
    verify_or_dummy,
)
from jobfinder.config import get_settings
from jobfinder.db import get_db
from jobfinder.models import Profile, Session, User
from jobfinder.notify import email as mail
from jobfinder.notify.tokens import (
    InvalidToken,
    make_password_token,
    password_fingerprint,
    read_password_token,
)

log = logging.getLogger("jobfinder.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])

LINK_SENT = "If that address has access, we've emailed a link. It expires in 60 minutes."
BAD_LINK = "This link is invalid or has expired. Request a new one from the sign-in page."


class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class LinkRequest(BaseModel):
    email: EmailStr


class ConfirmBody(BaseModel):
    token: str = Field(min_length=1, max_length=2048)
    password: str = Field(min_length=10, max_length=256)
    timezone: str | None = Field(default=None, max_length=64)  # from the browser; falls back to UTC


class UserOut(BaseModel):
    id: int
    email: str
    timezone: str
    digest_hour: int
    digest_enabled: bool
    is_admin: bool

    model_config = {"from_attributes": True}


def _valid_zone(name: str | None) -> str:
    try:
        ZoneInfo(name or "UTC")
        return name or "UTC"
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"


def _set_cookie(resp: Response, token: str) -> None:
    s = get_settings()
    resp.set_cookie(
        COOKIE_NAME,
        token,
        max_age=s.session_ttl_days * 86400,
        httponly=True,
        samesite="lax",
        secure=s.public_base_url.startswith("https"),
    )


def client_ip(request: Request) -> str:
    # uvicorn --proxy-headers (trusted via FORWARDED_ALLOW_IPS) already replaced this with nginx's real client IP.
    return request.client.host if request.client else "unknown"


def reset_link(user: User) -> str:
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/reset-password#token={make_password_token(user.id, user.password_hash)}"


async def _send_link(email: str, pending: bool, link: str) -> None:
    ttl = get_settings().password_link_ttl_minutes
    subject = "Set your JobFinder password" if pending else "Reset your JobFinder password"
    text = f"Use this link to {'set' if pending else 'reset'} your password. It works once and expires in {ttl} minutes.\n\n{link}\n\nIf you didn't ask for this, ignore this email."
    html = f'<p>Use this link to {"set" if pending else "reset"} your password. It works once and expires in {ttl} minutes.</p><p><a href="{link}">{"Set" if pending else "Reset"} your password</a></p><p>If you didn\'t ask for this, ignore this email.</p>'
    try:
        await mail.send_email(email, subject, text, html)
    except Exception:
        log.exception("password link email failed")


@router.post("/login", response_model=UserOut)
async def login(body: LoginBody, request: Request, resp: Response, db: Annotated[AsyncSession, Depends(get_db)]):
    s = get_settings()
    email, ip = body.email.lower(), client_ip(request)
    window = s.login_failure_window_minutes * 60
    if (
        await ratelimit.count_since(db, "login_fail_email", email, window) >= s.login_failures_per_email
        or await ratelimit.count_since(db, "login_fail_ip", ip, window) >= s.login_failures_per_ip
    ):
        raise HTTPException(429, "Too many attempts. Try again later.")
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    ok = await verify_or_dummy(user.password_hash if user else None, body.password)
    if not user or not ok:
        await ratelimit.hit(db, "login_fail_email", email)
        await ratelimit.hit(db, "login_fail_ip", ip)
        raise HTTPException(401, "Invalid email or password")
    user.is_admin = is_admin_email(user.email)
    await db.commit()
    _set_cookie(resp, await create_session(db, user.id))
    return user


@router.post("/password-reset/request", status_code=202)
async def request_link(
    body: LinkRequest, request: Request, background: BackgroundTasks, db: Annotated[AsyncSession, Depends(get_db)]
):
    """Same answer whether or not the address exists; only existing rows get mail."""
    s = get_settings()
    email, ip = body.email.lower(), client_ip(request)
    limited = (
        await ratelimit.count_since(db, "link_ip", ip, 3600) >= s.link_requests_per_ip_hour
        or await ratelimit.count_since(db, "link_email", email, 3600) >= s.link_requests_per_email_hour
    )
    last = await ratelimit.last_at(db, "link_email", email)
    if last and datetime.now(UTC) - last < timedelta(seconds=s.link_cooldown_seconds):
        limited = True
    await ratelimit.hit(db, "link_ip", ip)
    await ratelimit.hit(db, "link_email", email)
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user and not limited:
        background.add_task(_send_link, user.email, user.password_hash is None, reset_link(user))
    return {"detail": LINK_SENT}


@router.post("/password-reset/confirm", response_model=UserOut)
async def confirm_reset(body: ConfirmBody, resp: Response, db: Annotated[AsyncSession, Depends(get_db)]):
    try:
        uid, fp = read_password_token(body.token)
    except InvalidToken:
        raise HTTPException(400, BAD_LINK) from None
    user = await db.get(User, uid)
    if not user or fp != password_fingerprint(user.password_hash):
        raise HTTPException(400, BAD_LINK)
    if body.password.lower() == user.email.lower():
        raise HTTPException(422, "Password can't be your email address")
    pending = user.password_hash is None
    user.password_hash = await hash_password_async(body.password)
    if pending:
        user.timezone = _valid_zone(body.timezone)
        user.digest_hour, user.digest_enabled, user.question_emails_enabled = 8, True, True
        user.activated_at = datetime.now(UTC)
        if not await db.get(Profile, user.id):
            db.add(Profile(user_id=user.id, status={}, background={}, version=1))
    user.is_admin = is_admin_email(user.email)
    await db.execute(delete(Session).where(Session.user_id == user.id))
    _set_cookie(resp, await create_session(db, user.id))  # commits everything above
    return user


@router.post("/logout", status_code=204)
async def logout(
    resp: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    session: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
):
    if session:
        await db.execute(delete(Session).where(Session.token == hash_session_token(session)))
        await db.commit()
    resp.delete_cookie(COOKIE_NAME)


@router.get("/me", response_model=UserOut)
async def me(user: Annotated[User, Depends(current_user)]):
    return user
