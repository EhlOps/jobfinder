# Sketch: backend/jobfinder/tasks/ (D5). Replaces scheduling/handlers.HANDLERS, the poll loop in
# worker.py and its copy in renderer/__main__.py. The tasks table and its kinds are unchanged.

# ---------------------------------------------------------------- tasks/registry.py
@dataclass(frozen=True)
class TaskSpec:
    kind: str
    handler: Callable[..., Awaitable[dict]]
    payload: type[TaskPayload] | None
    queue: str = "default"            # "render" = the Playwright container
    retry_on: tuple[type[Exception], ...] = (AIError,)  # only when e.retryable


_REGISTRY: dict[str, TaskSpec] = {}


def task(kind: str, *, payload=None, queue="default"):
    def deco(fn):
        if kind in _REGISTRY:
            raise RuntimeError(f"task kind {kind!r} registered twice")
        _REGISTRY[kind] = TaskSpec(kind, fn, payload, queue)
        return fn
    return deco


def autodiscover() -> None:
    """Import every features/*/tasks.py once at startup so the decorators run."""
    for mod in pkgutil.iter_modules(features.__path__):
        importlib.import_module(f"jobfinder.features.{mod.name}.tasks")  # ModuleNotFoundError is fine


def kinds(queue: str) -> list[str]:
    return [s.kind for s in _REGISTRY.values() if s.queue == queue]


# ---------------------------------------------------------------- tasks/runner.py
class Runner:
    """The one poll loop. worker: Runner(ctx, queue="default"); renderer: Runner(ctx, queue="render")."""

    def __init__(self, ctx: AppContext, *, queue: str, concurrency: int, maintenance: bool = True): ...

    async def run_forever(self) -> None: ...          # the flowchart in diagrams/06-flow.png

    async def run_one(self, task_id: int) -> None:
        spec = registry.get(kind)
        async with self.ctx.sessions() as db:
            try:
                async with db.begin():                 # handler writes + task completion: one commit
                    task = await db.get(Task, task_id)
                    payload = spec.payload.model_validate(task.payload) if spec.payload else None
                    result = await spec.handler(TaskContext(self.ctx, db, task), payload)
                    queue.complete(task, result)
            except AIError as e:
                await self._fail(task_id, e, retry=e.retryable)      # side_session, backoff 30s/2m/8m
            except Exception as e:
                await self._fail(task_id, e, retry=False)


# ---------------------------------------------------------------- tasks/schedules.py
@dataclass(frozen=True)
class Schedule:
    kind: str
    trigger: dict                      # {"cron": "5 * * * *"} or {"minutes": 15}
    enabled: Callable[[Settings], bool] = lambda s: True


def schedule(*, cron: str | None = None, every: dict | None = None, enabled=lambda s: True):
    """Declared next to the task it enqueues, e.g. features/digest/tasks.py."""
    ...


# ---------------------------------------------------------------- worker.py (whole file)
async def main() -> None:
    ctx = build_context(Settings())
    registry.autodiscover()
    schedules.start(ctx)                    # APScheduler: enqueue(kind, dedupe=True) on each trigger
    await Runner(ctx, queue="default", concurrency=ctx.settings.ai.max_concurrency).run_forever()
