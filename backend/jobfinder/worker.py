"""Worker: consumes the Postgres task queue. Scheduled jobs (ingest, digest) are added later."""
import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from jobfinder.ai.claude_code import AIError, CallRecord, ClaudeCode
from jobfinder.ai.tasks import AITasks
from jobfinder.auth import ratelimit
from jobfinder.auth.security import prune_sessions
from jobfinder.config import get_settings
from jobfinder.db import SessionLocal
from jobfinder.models import AICall, Task
from jobfinder.scheduling import queue
from jobfinder.scheduling.handlers import HANDLERS

log = logging.getLogger("jobfinder.worker")
POLL_SECONDS = 2
HEARTBEAT_SECONDS = 60  # keep locked_at fresh while a task runs (queue.STUCK_AFTER is 5 minutes)
REAP_SECONDS = 60


async def record_call(rec: CallRecord) -> None:
    async with SessionLocal() as db:
        db.add(AICall(**rec.__dict__))
        await db.commit()


async def _heartbeat_loop(task_id: int, session_factory) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            async with session_factory() as db:
                await queue.heartbeat(db, task_id)
        except Exception:
            log.exception("heartbeat for task %s failed", task_id)


async def _fail(session_factory, task_id: int, error: str, *, retry: bool) -> None:
    """Record a failure on a fresh session: the handler's session may be in a failed transaction."""
    try:
        async with session_factory() as db:
            task = await db.get(Task, task_id)
            if task:
                await queue.fail(db, task, error, retry=retry)
    except Exception:
        log.exception("could not record failure of task %s; the reaper will recover it", task_id)


async def run_task(task_id: int, ai: AITasks, session_factory=SessionLocal) -> None:
    async with session_factory() as db:
        task = await db.get(Task, task_id)
        if task is None:
            log.warning("task %s no longer exists", task_id)
            return
        kind = task.kind
        handler = HANDLERS.get(kind)
        beat = asyncio.create_task(_heartbeat_loop(task_id, session_factory))
        try:
            if handler is None:
                raise ValueError(f"No handler for task kind {kind!r}")
            result = await handler(db, ai, task)
            await queue.complete(db, task, result)
        except AIError as e:
            log.warning("task %s (%s) AI error: %s", task_id, kind, e)
            await db.rollback()
            await _fail(session_factory, task_id, str(e), retry=e.retryable)
        except Exception as e:
            log.exception("task %s (%s) failed", task_id, kind)
            await db.rollback()
            await _fail(session_factory, task_id, f"{type(e).__name__}: {e}", retry=False)
        finally:
            beat.cancel()


def _log_task_exception(t: asyncio.Task) -> None:
    if not t.cancelled() and t.exception() is not None:
        log.error("task runner crashed", exc_info=t.exception())


async def enqueue_scheduled(kind: str) -> None:
    """Scheduled work goes through the same queue as everything else (retries, one at a time via dedupe)."""
    async with SessionLocal() as db:
        await queue.enqueue(db, kind, dedupe=True)
    log.info("scheduled %s", kind)


def make_scheduler() -> AsyncIOScheduler:
    s = get_settings()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        enqueue_scheduled, "interval", hours=s.ingest_interval_hours, args=["ingest_jobs"], id="ingest_jobs",
        next_run_time=datetime.now(UTC) + timedelta(minutes=2), coalesce=True, max_instances=1,
    )
    scheduler.add_job(
        enqueue_scheduled, "interval", minutes=s.planner_interval_minutes, args=["plan_work"], id="plan_work",
        next_run_time=datetime.now(UTC) + timedelta(minutes=5), coalesce=True, max_instances=1,
    )
    scheduler.add_job(enqueue_scheduled, "cron", minute=5, args=["daily_emails"], id="daily_emails", coalesce=True, max_instances=1)
    return scheduler


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ai = AITasks(ClaudeCode(recorder=record_call))
    limit = get_settings().ai_max_concurrency
    running: set[asyncio.Task] = set()
    if get_settings().scheduler_enabled:
        make_scheduler().start()
        log.info(
            "scheduler started: ingest every %dh, planner every %dm, daily emails hourly at :05",
            get_settings().ingest_interval_hours, get_settings().planner_interval_minutes,
        )
    log.info("worker started (concurrency=%d)", limit)
    last_reap = 0.0
    while True:
        running = {t for t in running if not t.done()}
        if time.monotonic() - last_reap >= REAP_SECONDS:
            last_reap = time.monotonic()
            try:
                async with SessionLocal() as db:
                    await queue.reap_stuck(db)
                    await ratelimit.prune(db)
                await prune_sessions(db)
            except Exception:
                log.exception("maintenance tick failed")
        claimed = None
        if len(running) < limit:
            async with SessionLocal() as db:
                claimed = await queue.claim(db)
        if claimed:
            t = asyncio.create_task(run_task(claimed.id, ai))
            t.add_done_callback(_log_task_exception)
            running.add(t)
            continue  # look for more work immediately
        await asyncio.sleep(POLL_SECONDS)


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
