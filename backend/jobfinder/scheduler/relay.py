"""The relay: every RELAY_SECONDS, publish the queued, due rows valkey never got.

The ledger publishes a row right after its transaction commits; this catches what that missed: a
publish that failed (valkey down), a process that died between commit and publish, and rows that only
became due later (retries after a backoff, staggered planner runs). Rows relayed long ago and still
queued are sent again (ledger.RELAY_RESEND). Duplicates are harmless: workers claim by id atomically.
"""
import logging
from datetime import datetime

from jobfinder import db as dbmod
from jobfinder import scheduler as pkg
from jobfinder.core.clock import utcnow
from jobfinder.tasks import brokers, ledger
from jobfinder.tasks.kinds import QUEUES

log = logging.getLogger("jobfinder.scheduler")


async def tick(now: datetime | None = None) -> int:
    """One relay pass; returns how many rows it published. Never raises."""
    try:
        return await _relay(now or utcnow())
    except Exception:
        log.exception("relay tick failed; the next one will try again")
        return 0
    finally:
        _touch_alive()


async def _relay(now: datetime) -> int:
    async with dbmod.SessionLocal() as db:  # read, then let go: no transaction stays open across publishes
        rows = await ledger.due_for_relay(db, now)
    sent: list[int] = []
    failed: dict[str, Exception] = {}  # queue -> its first error; that queue's other rows wait a tick
    for task_id, queue, kind in rows:
        # The kind decides the stream: a row queued before its kind moved keeps the old column.
        queue = QUEUES.get(kind, queue)
        if queue in failed:
            continue
        try:  # within brokers.PUBLISH_TIMEOUT, so a valkey that hangs cannot stall the tick
            await brokers.publish(queue, kind, task_id)
        except Exception as e:
            failed[queue] = e
            continue
        sent.append(task_id)
    if failed:  # one warning per tick, however many rows are waiting
        log.warning("relay: publish failed (%s), %d task(s) wait for the next tick",
                    "; ".join(f"{q}: {type(e).__name__}: {e}" for q, e in failed.items()), len(rows) - len(sent))
    if sent:
        async with dbmod.SessionLocal() as db:
            await ledger.mark_sent(db, sent, now)
            await db.commit()
        log.info("relay: published %d task(s)", len(sent))
    return len(sent)


def _touch_alive() -> None:
    try:
        pkg.ALIVE_FILE.touch()
    except OSError as e:
        log.warning("could not touch %s: %s", pkg.ALIVE_FILE, e)
