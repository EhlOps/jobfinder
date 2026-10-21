"""Maintenance, every WORKER_REAP_SECONDS: reap tasks whose worker died (settling what one that failed for
good leaves behind, tasks.failures), prune finished tasks older than
TASK_PRUNE_DAYS, old rate-limit events and expired sessions. One transaction per tick; a failure is logged
and the next tick tries again."""
import logging
from functools import partial

from jobfinder.core.config import settings
from jobfinder.core.context import default_context
from jobfinder.services import ratelimit
from jobfinder.services.auth import prune_sessions
from jobfinder.tasks import failures, ledger

log = logging.getLogger("jobfinder.scheduler")


async def tick() -> None:
    try:
        async with default_context().sessions() as db:
            requeued = await ledger.reap(db, on_failed=partial(failures.settle, store=False))
            pruned = await ledger.prune(db)
            await ratelimit.prune(db)
            await prune_sessions(db)
            await db.commit()
    except Exception:
        log.exception("maintenance tick failed")
        return
    if pruned:
        log.info("pruned %d finished task(s) older than %d days", pruned, settings.task_prune_days)
    if requeued:
        log.warning("requeued %d task(s) whose worker stopped responding", requeued)
