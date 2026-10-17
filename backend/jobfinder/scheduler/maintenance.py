"""Maintenance, every WORKER_REAP_SECONDS: reap tasks whose worker died, prune old rate-limit events and
expired sessions. One transaction per tick; a failure is logged and the next tick tries again."""
import logging

from jobfinder import db as dbmod
from jobfinder.auth.security import prune_sessions
from jobfinder.services import ratelimit
from jobfinder.tasks import ledger

log = logging.getLogger("jobfinder.scheduler")


async def tick() -> None:
    try:
        async with dbmod.SessionLocal() as db:
            requeued = await ledger.reap(db)
            await ratelimit.prune(db)
            await prune_sessions(db)
            await db.commit()
    except Exception:
        log.exception("maintenance tick failed")
        return
    if requeued:
        log.warning("requeued %d task(s) whose worker stopped responding", requeued)
