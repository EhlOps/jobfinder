"""`python -m jobfinder.scheduler`: the cron table (when SCHEDULER_ENABLED), the relay and maintenance on
one AsyncIOScheduler (UTC), until SIGTERM. Refuses to start unless TASKS_BACKEND=ledger."""
import asyncio
import logging
import signal
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from jobfinder import db as dbmod
from jobfinder.core import logging as core_logging
from jobfinder.core.clock import utcnow
from jobfinder.core.config import settings
from jobfinder.scheduler import cron, maintenance, relay

log = logging.getLogger("jobfinder.scheduler")
SHUTDOWN_GRACE = 10  # seconds after SIGTERM for work in flight; well inside k8s's default 30s


def build() -> AsyncIOScheduler:
    scheduler = cron.make_scheduler() if settings.scheduler_enabled else cron.new_scheduler()
    now = utcnow()  # both run at once on start: a restart re-sends and reaps without waiting an interval
    scheduler.add_job(relay.tick, "interval", seconds=settings.relay_seconds, id="relay", next_run_time=now)
    scheduler.add_job(maintenance.tick, "interval", seconds=settings.worker_reap_seconds, id="maintenance",
                      next_run_time=now)
    return scheduler


async def serve() -> None:
    already = asyncio.all_tasks()  # whoever started us; not ours to wait for on the way out
    scheduler = build()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    scheduler.start()
    for job in scheduler.get_jobs():
        log.info("scheduler: %s %s", job.id, job.trigger)
    if not settings.scheduler_enabled:
        log.info("scheduler: SCHEDULER_ENABLED is off, so no cron jobs; relay and maintenance only")
    await stop.wait()
    scheduler.pause()  # no new runs
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.remove_signal_handler(sig)
    # Let ticks and after-commit publishes in flight finish (shutdown would cancel them). One cut off
    # after the grace rolls back, and whatever it was working on is still in the ledger for the next start.
    running = asyncio.all_tasks() - already - {asyncio.current_task()}
    if running:
        await asyncio.wait(running, timeout=SHUTDOWN_GRACE)
    scheduler.shutdown(wait=False)
    await dbmod.engine.dispose()
    log.info("scheduler stopped")


def main() -> int:
    core_logging.configure(settings.log_level)
    # APScheduler logs every run at INFO; with the relay every few seconds that drowns everything else.
    logging.getLogger("apscheduler.executors").setLevel(max(logging.WARNING, logging.getLogger().level))
    if settings.tasks_backend != "ledger":
        log.error("TASKS_BACKEND=%s: the legacy worker runs the cron and the reaper itself; not starting a "
                  "second scheduler (set TASKS_BACKEND=ledger)", settings.tasks_backend)
        return 2
    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
