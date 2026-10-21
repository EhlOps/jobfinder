"""The FastAPI app factory: `uvicorn jobfinder.api.app:create_app --factory`.

Settings and the context (jobfinder/context.py) are resolved when the app is created, so a missing key
stops startup before anything is served. Building the context does no I/O; tests pass their own.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from jobfinder.api import (
    ai_admin,
    cover_letters,
    coverage,
    documents,
    health,
    interview,
    job_links,
    links,
    matches,
    onboarding,
    profile,
    questions,
    resumes,
    tasks,
)
from jobfinder.api import auth as auth_routes
from jobfinder.api import settings as settings_api
from jobfinder.context import AppContext, build_context, default_context
from jobfinder.core import errors
from jobfinder.core.config import Settings
from jobfinder.services import health as health_service

ROUTERS = (
    auth_routes.router,
    profile.router,
    interview.router,
    documents.router,
    links.router,
    job_links.router,
    onboarding.router,
    tasks.router,
    ai_admin.router,
    coverage.router,
    matches.router,
    cover_letters.router,
    resumes.router,
    questions.router,
    settings_api.router,
)


def create_app(settings: Settings | None = None, ctx: AppContext | None = None) -> FastAPI:
    """With `ctx` (tests), the app uses that context and leaves closing it to the caller. Without, it
    builds one from `settings`, or takes the process's own (`default_context`), and the lifespan
    closes it at shutdown. Either way the settings load here, so a missing key fails startup."""
    owned = ctx is None
    context = ctx or (default_context() if settings is None else build_context(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owned:
                await context.aclose()

    app = FastAPI(title="JobFinder", lifespan=lifespan)
    app.state.ctx = context
    app.state.alembic = health_service.alembic_revisions()
    errors.install(app)
    for router in ROUTERS:
        app.include_router(router)
    app.add_api_route("/healthz", health.healthz, methods=["GET"])
    app.add_api_route("/readyz", health.readyz, methods=["GET"])
    return app
