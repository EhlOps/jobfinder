# Sketch (v2): the task infrastructure (D6). Postgres keeps the ledger (the truth the UI, planner and
# admin pages read); valkey only delivers "this task id is due now"; the scheduler owns time.

# ---------------------------------------------------------------- migration (the only schema change)
#   ALTER TABLE tasks ADD COLUMN queue   varchar(16) NOT NULL DEFAULT 'io';
#   ALTER TABLE tasks ADD COLUMN sent_at timestamptz;
#   UPDATE tasks SET queue = 'ai'     WHERE kind IN (...ai kinds...);
#   UPDATE tasks SET queue = 'render' WHERE kind LIKE 'render\_%';
#   CREATE INDEX tasks_due ON tasks (queue, run_after) WHERE status = 'queued';

# ---------------------------------------------------------------- tasks/brokers.py
from taskiq_redis import RedisStreamBroker      # valkey speaks the Redis protocol

from jobfinder.core.config import Settings

_url = Settings().valkey.url
ai_broker = RedisStreamBroker(url=_url, queue_name="ai")
io_broker = RedisStreamBroker(url=_url, queue_name="io")
render_broker = RedisStreamBroker(url=_url, queue_name="render")
BROKERS = {"ai": ai_broker, "io": io_broker, "render": render_broker}


# ---------------------------------------------------------------- tasks/ledger.py
async def add(db, kind: str, *, queue: str, payload: dict | None = None, user_id: int | None = None,
              run_after=None, dedupe: bool = False) -> Task:
    """Insert (or, with dedupe, reuse) a queued row. Flushes only: the caller's transaction commits it.
    The api's after-commit hook, or else the scheduler's relay, publishes it to valkey."""


async def claim(db, task_id: int) -> Task | None:
    """queued -> running, atomically. None when another delivery already took it (duplicates are normal)."""
    return await db.scalar(
        sa.update(Task).where(Task.id == task_id, Task.status == "queued")
        .values(status="running", locked_at=clock.now(), attempts=Task.attempts + 1).returning(Task)
    )


# ---------------------------------------------------------------- tasks/runtime.py
def ledger_task(broker, kind: str, *, payload: type[TaskPayload] | None = None):
    """Registers `kind` -> handler and a taskiq task that receives only {task_id}. On delivery:
    claim (or drop a duplicate), validate the payload, run the handler and complete the row in ONE
    transaction, then ack. A retryable AIError rolls back and sets run_after (30s / 2m / 8m); the relay
    re-sends it when due. See diagrams/06-flow.png."""


# ---------------------------------------------------------------- scheduler/__main__.py  (container: scheduler, 1 replica)
async def main() -> None:
    ctx = build_context(Settings())
    s = AsyncIOScheduler(timezone="UTC")
    for job in CRON:                                       # scheduler/cron.py: the one cron table
        s.add_job(enqueue_cron, job.trigger, args=[ctx, job.kind, job.queue], id=job.kind, coalesce=True)
    s.add_job(relay, "interval", seconds=ctx.settings.scheduler.relay_seconds, args=[ctx])      # due rows -> XADD
    s.add_job(reap, "interval", seconds=ctx.settings.scheduler.reap_seconds, args=[ctx])        # stale running -> queued
    s.add_job(prune, "interval", minutes=10, args=[ctx])                                        # sessions, rate events
    s.start()
    await asyncio.Event().wait()


# scheduler/cron.py
CRON = [
    Cron("ingest_tick", "io", {"minutes": 15}),          # fans out one ingest_board per due board
    Cron("plan_work", "io", {"minutes": 15}),
    Cron("daily_emails", "io", {"cron": "5 * * * *"}),
    Cron("discover_boards", "io", {"cron": "30 4 * * sun"}, enabled="DISCOVERY_ENABLED"),
    Cron("score_coveted", "io", {"cron": "0 6 * * sun"}, enabled="COVERAGE_ENABLED"),
]


# ---------------------------------------------------------------- docker-compose.yml additions (sketch)
#   valkey:
#     image: valkey/valkey:8
#     command: ["valkey-server", "--appendonly", "yes"]
#     volumes: [valkey:/data]
#   scheduler:
#     build: ./backend
#     command: ["python", "-m", "jobfinder.scheduler"]
#   worker-io:
#     build: ./backend
#     command: ["taskiq", "worker", "jobfinder.tasks.brokers:io_broker", "jobfinder.tasks.io", "--max-async-tasks", "10"]
#   worker-ai:
#     build: {context: ./backend, dockerfile: Dockerfile.worker}      # the only image with the claude CLI
#     command: ["taskiq", "worker", "jobfinder.tasks.brokers:ai_broker", "jobfinder.tasks.ai", "--max-async-tasks", "2"]
#   renderer:
#     command: ["taskiq", "worker", "jobfinder.tasks.brokers:render_broker", "jobfinder.tasks.render", "--max-async-tasks", "2"]
