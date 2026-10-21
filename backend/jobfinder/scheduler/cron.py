"""The cron table: which kinds run on a clock, when, and behind which setting.

Each run adds one deduped ledger row and commits, so a scheduled run shows up and retries like any
other task and never piles up behind a slow previous one. This is the only copy of the table (#53).
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from jobfinder.core.clock import GRID_ANCHOR, next_on_grid, utcnow
from jobfinder.core.config import Settings, settings
from jobfinder.core.context import default_context
from jobfinder.tasks import ledger

log = logging.getLogger("jobfinder.scheduler")
# A run late by up to the grace (a busy loop, a slow tick) still fires, and a backlog fires once. The job
# store is in memory, so runs that fell while the process was down are not replayed.
JOB_DEFAULTS = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}


def _always(s: Settings) -> bool:
    return True


@dataclass(frozen=True)
class Cron:
    kind: str
    trigger: str  # an APScheduler trigger alias: "interval" or "cron"
    when: Callable[[Settings], dict]  # the trigger's arguments, read from settings when the jobs are added
    first_run: timedelta | None = None  # interval jobs: first run this long after start, not one interval
    anchor: datetime | None = None  # interval jobs: fire on anchor + k * interval (next_on_grid), not from start
    enabled: Callable[[Settings], bool] = _always


def _ingest_every(s: Settings) -> dict:
    # adaptive cadence: tick often, each run fetches only the boards that are due (services.ingest_schedule)
    return {"minutes": s.ingest_tick_minutes} if s.adaptive_schedule_enabled else {"hours": s.ingest_interval_hours}


TABLE = [
    Cron("ingest_tick", "interval", _ingest_every, first_run=timedelta(minutes=2)),
    # on the grid, so the admin schedule page can say when the next tick is (services/admin.py)
    Cron("plan_work", "interval", lambda s: {"minutes": s.planner_interval_minutes}, first_run=timedelta(minutes=5),
         anchor=GRID_ANCHOR),
    # hourly; each user's lands at their own hour, and it queues question collection ahead of it (#50)
    Cron("daily_emails", "cron", lambda s: {"minute": 5}),
    Cron("discover_boards", "cron", lambda s: {"day_of_week": "sun", "hour": 4, "minute": 30},
         enabled=lambda s: s.discovery_enabled),
    # after discovery, so the outside-list signal is fresh
    Cron("score_coveted", "cron", lambda s: {"day_of_week": "sun", "hour": 6, "minute": 0},
         enabled=lambda s: s.coverage_enabled),
]


async def run(kind: str) -> None:
    """One scheduled run: queue the kind (unless it's already queued or running) and commit."""
    try:
        async with default_context().sessions() as db:
            task = await ledger.add(db, kind, dedupe=True)
            await db.commit()
    except Exception:
        log.exception("scheduling %s failed; its next run will try again", kind)
        return
    log.info("scheduled %s (task %s)", kind, task.id)


def new_scheduler() -> AsyncIOScheduler:
    return AsyncIOScheduler(timezone="UTC", job_defaults=JOB_DEFAULTS)


def add_jobs(scheduler: AsyncIOScheduler) -> AsyncIOScheduler:
    """Add every enabled cron job, its trigger computed from the current settings."""
    s = settings
    for c in TABLE:
        if not c.enabled(s):
            continue
        when, first = c.when(s), utcnow() + c.first_run if c.first_run else None
        if c.anchor is not None:  # the first grid point after the delay; later runs stay on the grid
            first = next_on_grid(first or utcnow(), timedelta(**when), c.anchor)
            when = {**when, "start_date": c.anchor}
        start = {"next_run_time": first} if first else {}
        scheduler.add_job(run, c.trigger, **when, **start, args=[c.kind], id=c.kind)
    return scheduler


def make_scheduler() -> AsyncIOScheduler:
    return add_jobs(new_scheduler())
