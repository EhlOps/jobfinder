"""The dependencies routers declare, defined once: `db: DbSession`, `user: CurrentUser`, `ctx: Ctx`.

    DbSession    the request's session; committed when the handler returns (core/db.py)
    CurrentUser  the signed-in user, or 401
    AdminUser    the signed-in admin, or 403
    Ctx          the app context (engine, HTTP client, clock, settings, Claude, document store)
    Store        the context's document store
"""
from typing import Annotated

from fastapi import Depends, Request

from jobfinder.auth.security import current_user, require_admin
from jobfinder.context import AppContext
from jobfinder.core.db import DbSession
from jobfinder.models import User
from jobfinder.storage.documents import DocumentStore

__all__ = ["AdminUser", "Ctx", "CurrentUser", "DbSession", "Store"]


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_ctx_store(request: Request) -> DocumentStore:
    return get_ctx(request).store


CurrentUser = Annotated[User, Depends(current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
Ctx = Annotated[AppContext, Depends(get_ctx)]
Store = Annotated[DocumentStore, Depends(get_ctx_store)]
