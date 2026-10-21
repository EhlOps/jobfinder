"""The app context's infrastructure: what one process shares, built in exactly one place.

`build_context(settings)` makes the engine, the session factory, the shared HTTP client and the
clock. Nothing is built at import time. core may import nothing outside core, so the integrations
(`ctx.claude`, `ctx.store`) are added one layer up, in jobfinder/context.py, whose AppContext extends
this one; the api, the worker and the CLI use that. `default_context()` here is the process's own
infrastructure: code outside a request (the task runtime, CLI, renderer, scheduler) opens its sessions
from `default_context().sessions`, and jobfinder/context.py builds on it. Tests build theirs around the
test database and swap the default context's `sessions` for it.
"""
from dataclasses import dataclass
from functools import lru_cache

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from jobfinder.core.clock import Clock, utcnow
from jobfinder.core.config import Settings, get_settings
from jobfinder.core.db import Sessions, database_url, make_engine, make_sessions
from jobfinder.core.http import new_client


@dataclass
class AppContext:
    settings: Settings
    engine: AsyncEngine
    sessions: Sessions
    http: httpx.AsyncClient
    clock: Clock

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.engine.dispose()


def build_context(
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    sessions: Sessions | None = None,
    http: httpx.AsyncClient | None = None,
    clock: Clock | None = None,
) -> AppContext:
    """Build what is not passed in. Tests pass their own engine and sessions."""
    engine = engine or make_engine(database_url(settings))
    return AppContext(
        settings=settings,
        engine=engine,
        sessions=sessions or make_sessions(engine),
        http=http or new_client(),
        clock=clock or utcnow,
    )


@lru_cache
def default_context() -> AppContext:
    """This process's context, from its settings, built on first use."""
    return build_context(get_settings())
