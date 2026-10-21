"""The poll-loop worker (TASKS_BACKEND=legacy): claims tasks straight from the Postgres table and runs
its own APScheduler cron. With TASKS_BACKEND=ledger it refuses to start: the taskiq workers
(`python -m jobfinder.tasks ai|io`) and the scheduler (`python -m jobfinder.scheduler`) replace it (#48).
"""
import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from jobfinder.auth.security import prune_sessions
from jobfinder.context import default_context
from jobfinder.core import logging as core_logging
from jobfinder.core.config import settings
from jobfinder.core.context import default_context as core_context
from jobfinder.integrations.claude.claude_code import AIError
from jobfinder.integrations.claude.tasks import AITasks
from jobfinder.models import Task
from jobfinder.scheduling import queue
from jobfinder.scheduling.handlers import HANDLERS
from jobfinder.services import ratelimit
from jobfinder.tasks import ledger

log = logging.getLogger("jobfinder.worker")
# Loop timings come from settings: WORKER_POLL_SECONDS, TASK_HEARTBEAT_SECONDS (keeps locked_at fresh
# while a task runs; must stay well under TASK_STUCK_AFTER_MINUTES) and WORKER_REAP_SECONDS.


async def _heartbeat_loop(task_id: int, session_factory) -> None:
    while True:
        await asyncio.sleep(settings.task_heartbeat_seconds)
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


async def run_task(task_id: int, ai: AITasks, session_factory=None) -> None:
    session_factory = session_factory or core_context().sessions
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
    async with core_context().sessions() as db:
        await ledger.add(db, kind, dedupe=True)  # the ledger sets the row's queue, ready for the switch
        await db.commit()
    log.info("scheduled %s", kind)


def make_scheduler() -> AsyncIOScheduler:
    s = settings
    scheduler = AsyncIOScheduler(timezone="UTC")
    # adaptive cadence: tick often, each run fetches only the boards that are due (ingest.schedule)
    every = (
        {"minutes": s.ingest_tick_minutes} if s.adaptive_schedule_enabled else {"hours": s.ingest_interval_hours}
    )
    scheduler.add_job(
        enqueue_scheduled, "interval", **every, args=["ingest_tick"], id="ingest_tick",
        next_run_time=datetime.now(UTC) + timedelta(minutes=2), coalesce=True, max_instances=1,
    )
    scheduler.add_job(
        enqueue_scheduled, "interval", minutes=s.planner_interval_minutes, args=["plan_work"], id="plan_work",
        next_run_time=datetime.now(UTC) + timedelta(minutes=5), coalesce=True, max_instances=1,
    )
    scheduler.add_job(enqueue_scheduled, "cron", minute=5, args=["daily_emails"], id="daily_emails", coalesce=True, max_instances=1)
    if s.discovery_enabled:
        scheduler.add_job(
            enqueue_scheduled, "cron", day_of_week="sun", hour=4, minute=30, args=["discover_boards"],
            id="discover_boards", coalesce=True, max_instances=1,
        )
    if s.coverage_enabled:  # after discovery, so the outside-list signal is fresh
        scheduler.add_job(
            enqueue_scheduled, "cron", day_of_week="sun", hour=6, minute=0, args=["score_coveted"],
            id="score_coveted", coalesce=True, max_instances=1,
        )
    return scheduler


LEDGER_REFUSAL = ("TASKS_BACKEND=ledger: run python -m jobfinder.tasks ai|io and python -m jobfinder.scheduler "
                  "instead")


async def main() -> None:
    core_logging.configure(settings.log_level)
    if settings.tasks_backend == "ledger":  # the taskiq workers own the queued rows now
        log.error(LEDGER_REFUSAL)
        raise SystemExit(1)
    ai = default_context().claude  # records every call in ai_calls
    limit = settings.ai_max_concurrency
    running: set[asyncio.Task] = set()
    if settings.scheduler_enabled:
        make_scheduler().start()
        if settings.discovery_enabled:
            log.info("board discovery scheduled weekly (Sunday 04:30 UTC)")
        s = settings
        ingest = (
            f"adaptive (tick every {s.ingest_tick_minutes}m)" if s.adaptive_schedule_enabled
            else f"every {s.ingest_interval_hours}h"
        )
        log.info(
            "scheduler started: ingest %s, planner every %dm, daily emails hourly at :05",
            ingest, s.planner_interval_minutes,
        )
    log.info("worker started (concurrency=%d)", limit)
    last_reap = 0.0
    while True:
        running = {t for t in running if not t.done()}
        if time.monotonic() - last_reap >= settings.worker_reap_seconds:
            last_reap = time.monotonic()
            try:
                async with core_context().sessions() as db:
                    await queue.reap_stuck(db)
                    pruned = await ledger.prune(db)
                    await ratelimit.prune(db)
                    await prune_sessions(db)
                    await db.commit()
                if pruned:
                    log.info("pruned %d finished task(s) older than %d days", pruned, settings.task_prune_days)
            except Exception:
                log.exception("maintenance tick failed")
        claimed = None
        if len(running) < limit:
            async with core_context().sessions() as db:
                claimed = await queue.claim(db, exclude_prefix=queue.RENDER_PREFIX)
        if claimed:
            t = asyncio.create_task(run_task(claimed.id, ai))
            t.add_done_callback(_log_task_exception)
            running.add(t)
            continue  # look for more work immediately
        await asyncio.sleep(settings.worker_poll_seconds)


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
