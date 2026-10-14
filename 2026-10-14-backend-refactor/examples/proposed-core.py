# Sketch: backend/jobfinder/core/ (D2, D3, D4). Each section is its own module.

# ---------------------------------------------------------------- core/config.py
# Groups keep today's env var names: each group has its own prefix, and the few odd names get an alias.
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV = dict(env_file=".env", extra="ignore")


class IngestSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INGEST_", **_ENV)
    tick_minutes: int = 15                      # INGEST_TICK_MINUTES (unchanged)
    min_interval_minutes: int = 60              # INGEST_MIN_INTERVAL_MINUTES (unchanged)
    concurrency: int = 10
    adaptive: bool = Field(True, validation_alias=AliasChoices("ADAPTIVE_SCHEDULE_ENABLED"))  # odd name kept


class Settings(BaseSettings):
    model_config = SettingsConfigDict(**_ENV)
    db: DbSettings = Field(default_factory=DbSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    ai: AISettings = Field(default_factory=AISettings)
    ingest: IngestSettings = Field(default_factory=IngestSettings)
    # ... matching, planner, digest, discovery, renderer, smtp, sources


# ---------------------------------------------------------------- core/context.py
from dataclasses import dataclass


@dataclass
class AppContext:
    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    http: httpx.AsyncClient          # one client: shared User-Agent, timeouts, SSRF-safe transport
    store: DocumentStore
    ai: AIClient
    clock: Clock

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.engine.dispose()


def build_context(settings: Settings, **overrides) -> AppContext:
    """The only place that builds infrastructure. app lifespan, worker, cli and tests all call this."""
    engine = overrides.pop("engine", None) or create_async_engine(settings.db.url, pool_pre_ping=True)
    ...


# ---------------------------------------------------------------- core/db.py
async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """One transaction per request: commit when the handler returns, roll back when it raises."""
    async with request.app.state.ctx.sessions() as session, session.begin():
        yield session


@asynccontextmanager
async def side_session(ctx: AppContext) -> AsyncIterator[AsyncSession]:
    """A short, separate transaction for writes that must survive the caller's rollback:
    login-failure rate events, AICall records, the failure record of a task."""
    async with ctx.sessions() as session, session.begin():
        yield session


# ---------------------------------------------------------------- core/errors.py
class AppError(Exception):
    status = 400
    code = "error"
    message = "Something went wrong"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.message)


class NotFound(AppError):  status, code = 404, "not_found"
class Forbidden(AppError): status, code = 403, "forbidden"
class Conflict(AppError):  status, code = 409, "conflict"
class Invalid(AppError):   status, code = 422, "invalid"
class RateLimited(AppError): status, code = 429, "rate_limited"


def install(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle(_: Request, e: AppError) -> JSONResponse:
        # `detail` is what the SPA reads today; `code` is new and optional for it
        return JSONResponse({"detail": str(e), "code": e.code}, status_code=e.status)


# ---------------------------------------------------------------- core/deps.py  (the only place these aliases live)
DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
Ctx = Annotated[AppContext, Depends(lambda request: request.app.state.ctx)]


# ---------------------------------------------------------------- app.py
def create_app(settings: Settings | None = None, ctx: AppContext | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.ctx = ctx or build_context(settings or Settings())
        yield
        await app.state.ctx.aclose()

    app = FastAPI(title="JobFinder", lifespan=lifespan)
    errors.install(app)
    for router in features.routers():  # each features/<name>/router.py, in a fixed list
        app.include_router(router)
    return app
