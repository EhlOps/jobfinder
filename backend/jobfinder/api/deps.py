"""The dependencies routers declare, defined once: `db: DbSession`, `user: CurrentUser`, `ctx: Ctx`.

    DbSession    the request's session; committed when the handler returns (core/db.py)
    CurrentUser  the signed-in user, or 401
    AdminUser    the signed-in admin, or 403
    Ctx          the app context (engine, HTTP client, clock, settings, Claude, document store)
    Store        the context's document store
"""
from typing import Annotated

from fastapi import Cookie, Depends, Request

from jobfinder.context import AppContext
from jobfinder.core.db import DbSession
from jobfinder.core.errors import Forbidden, Unauthorized
from jobfinder.models import User
from jobfinder.services import auth
from jobfinder.storage.documents import DocumentStore

__all__ = ["COOKIE_NAME", "AdminUser", "Ctx", "CurrentUser", "DbSession", "Store"]

COOKIE_NAME = "session"


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_ctx_store(request: Request) -> DocumentStore:
    return get_ctx(request).store


async def current_user(
    db: DbSession,
    session: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> User:
    user = await auth.session_user(db, session) if session else None
    if user is None:
        raise Unauthorized("Not authenticated")
    return user


async def require_admin(user: Annotated[User, Depends(current_user)]) -> User:
    if not user.is_admin:
        raise Forbidden("Admin only")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
Ctx = Annotated[AppContext, Depends(get_ctx)]
Store = Annotated[DocumentStore, Depends(get_ctx_store)]
