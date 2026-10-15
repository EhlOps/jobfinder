"""The dependencies routers declare, defined once: `db: DbSession`, `user: CurrentUser`, `ctx: Ctx`.

    DbSession    the request's session; committed when the handler returns (core/db.py)
    CurrentUser  the signed-in user, or 401
    AdminUser    the signed-in admin, or 403
    Ctx          the app context (engine, HTTP client, clock, settings)
"""
from typing import Annotated

from fastapi import Depends, Request

from jobfinder.auth.security import current_user, require_admin
from jobfinder.core.context import AppContext
from jobfinder.core.db import DbSession
from jobfinder.models import User

__all__ = ["AdminUser", "Ctx", "CurrentUser", "DbSession"]


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


CurrentUser = Annotated[User, Depends(current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
Ctx = Annotated[AppContext, Depends(get_ctx)]
