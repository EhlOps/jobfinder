from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, Request, Response

from jobfinder.auth.security import COOKIE_NAME, current_user
from jobfinder.core.config import settings
from jobfinder.core.db import DbSession
from jobfinder.models import User
from jobfinder.schemas.auth import ConfirmBody, LinkRequest, LoginBody, UserOut
from jobfinder.services import accounts

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _set_cookie(resp: Response, token: str) -> None:
    s = settings
    resp.set_cookie(
        COOKIE_NAME,
        token,
        max_age=s.session_ttl_days * 86400,
        httponly=True,
        samesite="lax",
        secure=s.public_base_url.startswith("https"),
    )


def client_ip(request: Request) -> str:
    # uvicorn --proxy-headers (trusted via FORWARDED_ALLOW_IPS) already replaced this with Caddy's real client IP.
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=UserOut)
async def login(body: LoginBody, request: Request, resp: Response, db: DbSession):
    user, token = await accounts.login(
        db, request.app.state.ctx.sessions, body.email, body.password, client_ip(request)
    )
    _set_cookie(resp, token)
    return user


@router.post("/password-reset/request", status_code=202)
async def request_link(
    body: LinkRequest, request: Request, background: BackgroundTasks, db: DbSession
):
    """Same answer whether or not the address exists; only existing rows get mail."""
    link = await accounts.request_link(db, body.email, client_ip(request))
    if link:
        background.add_task(accounts.send_link, link)  # runs after the request has committed
    return {"detail": accounts.LINK_SENT}


@router.post("/password-reset/confirm", response_model=UserOut)
async def confirm_reset(body: ConfirmBody, resp: Response, db: DbSession):
    user, token = await accounts.confirm_reset(db, body.token, body.password, body.timezone)
    _set_cookie(resp, token)
    return user


@router.post("/logout", status_code=204)
async def logout(
    resp: Response,
    db: DbSession,
    session: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
):
    await accounts.logout(db, session)
    resp.delete_cookie(COOKIE_NAME)


@router.get("/me", response_model=UserOut)
async def me(user: Annotated[User, Depends(current_user)]):
    return user
