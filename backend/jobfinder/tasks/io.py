"""The `io` queue's handlers: fetching and planning, no Claude. worker-io imports this module
(`python -m jobfinder.tasks io`), so a long ingest run never holds up an `ai` delivery: the two queues
are separate streams consumed by separate processes.

Handlers are `(db, ai, task, payload) -> dict` like tasks.ai's; `ai` is unused here.
"""
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import Task
from jobfinder.tasks import ledger
from jobfinder.tasks.brokers import io_broker
from jobfinder.tasks.runtime import ledger_task


class ScoreCovetedArgs(BaseModel):
    no_audit: bool = False  # the admin's "rescore only" skips the coverage audit that follows


PLAN_COALESCE = timedelta(minutes=1)  # boards finishing within this of each other share one plan_work


class IngestBoardArgs(BaseModel):
    company_id: int


class IngestSourceArgs(BaseModel):
    source: Literal["jobspy", "usajobs"]


@ledger_task(io_broker, "ingest_tick", ai=False)
async def ingest_tick(db: AsyncSession, ai, task: Task, _: None) -> dict:
    """The scheduled ingest (#50): pick the due boards (the adaptive cadence and the discovered-board cap)
    and queue one ingest_board per board, plus one ingest_source per due extra source, all committed
    with this task. Each board then runs, retries and logs on its own, worker-io's slots in parallel,
    so a slow or failing board holds up nothing else. Deduped: a board still queued or running from an
    earlier tick isn't queued twice.

    The legacy poll-loop worker runs AI_MAX_CONCURRENCY tasks at a time, so there the tick runs the
    whole thing inline, INGEST_CONCURRENCY boards at a time, as ingest_jobs did."""
    from jobfinder.core.config import settings
    from jobfinder.db import SessionLocal
    from jobfinder.ingest import runner
    from jobfinder.services import ingestion

    if settings.tasks_backend != "ledger":
        summary = await runner.run_ingest(SessionLocal)
        # Adaptive ticks come every few minutes and often fetch nothing: plan only when jobs changed.
        if not settings.adaptive_schedule_enabled or summary.get("new") or summary.get("deactivated"):
            await _plan(db)
        return summary
    ids = await ingestion.due_board_ids(db)
    for company_id in ids:
        await ledger.add(db, "ingest_board", {"company_id": company_id}, dedupe=True)
    # not recorded as run here: the ingest_source task records its run, so a tick that rolls back
    # doesn't skip the source for a whole interval
    sources = await runner.due_extras(SessionLocal, record=False)
    for source in sources:
        await ledger.add(db, "ingest_source", {"source": source}, dedupe=True)
    return {"due": len(ids), "sources": sources}


@ledger_task(io_broker, "ingest_jobs", ai=False)
async def ingest_jobs(db: AsyncSession, ai, task: Task, _: None) -> dict:
    """The old whole-run kind, kept so rows queued before the switch still run: a tick. TODO(#53): remove"""
    return await ingest_tick(db, ai, task, None)


@ledger_task(io_broker, "ingest_board", payload=IngestBoardArgs, ai=False)
async def ingest_board(db: AsyncSession, ai, task: Task, p: IngestBoardArgs) -> dict:
    """Fetch one board. The board commits in its own session (services.ingestion.ingest_board), which
    records a failed fetch on the board (its backoff is the board's retry); this task's session holds
    only the follow-up."""
    from jobfinder.db import SessionLocal
    from jobfinder.services import ingestion

    async with ingestion.http_client() as client:
        stats = await ingestion.ingest_board(SessionLocal, client, p.company_id)
    return await _changed(db, stats)


@ledger_task(io_broker, "ingest_source", payload=IngestSourceArgs, ai=False)
async def ingest_source(db: AsyncSession, ai, task: Task, p: IngestSourceArgs) -> dict:
    """Fetch jobspy or usajobs (the sources without boards), on their own so a long scrape never holds
    a board up. Like ingest_board, it commits in its own sessions (ingest.runner)."""
    from jobfinder.db import SessionLocal
    from jobfinder.ingest import runner
    from jobfinder.services import ratelimit

    await ratelimit.hit(db, runner.EXTRA_KIND, p.source)  # its cadence (runner._extra_due), kept with the result
    return await _changed(db, await runner.ingest_extra(SessionLocal, p.source))


async def _changed(db: AsyncSession, stats) -> dict:
    """The board's follow-up, as the whole run's was: plan when jobs changed (or always, off the adaptive
    schedule). Queued PLAN_COALESCE out, so the boards of one tick finishing together share one plan."""
    from jobfinder.core.clock import utcnow
    from jobfinder.core.config import settings

    if not settings.adaptive_schedule_enabled or stats.new or stats.deactivated:
        await _plan(db, run_after=utcnow() + PLAN_COALESCE)
    return stats.__dict__


async def _plan(db: AsyncSession, run_after=None) -> None:
    # Matching is planned, not fanned out: the planner picks who needs it most and staggers the runs.
    await ledger.add(db, "plan_work", run_after=run_after, rerun=True)  # a running plan missed these jobs


@ledger_task(io_broker, "plan_work", ai=False)
async def plan_work(db: AsyncSession, ai, task: Task, _: None) -> dict:
    from jobfinder.scheduling import planner

    return await planner.plan(db)


@ledger_task(io_broker, "daily_emails", ai=False)
async def daily_emails(db: AsyncSession, ai, task: Task, _: None) -> dict:
    """Hourly tick: queue question collection on `ai` for users whose digest is coming up, then send the
    daily email to every user whose chosen hour has come, with the questions collected so far (#50)."""
    from jobfinder.core.clock import utcnow
    from jobfinder.services import digest

    now = utcnow()
    queued = await digest.schedule_questions(db, now)
    await db.commit()  # the boundary: the collection rows, before the per-user sends commit their own
    return {**await digest.send_due(db, now), "questions_queued": queued}


@ledger_task(io_broker, "discover_boards", ai=False)
async def discover_boards(db: AsyncSession, ai, task: Task, _: None) -> dict:
    from jobfinder.ingest import discovery

    return (await discovery.discover_boards(db, checkpoint=db.commit)).as_dict()  # keep each board as it's added


@ledger_task(io_broker, "score_coveted", payload=ScoreCovetedArgs, ai=False)
async def score_coveted(db: AsyncSession, ai, task: Task, p: ScoreCovetedArgs) -> dict:
    """Weekly: recompute how sought-after each employer is, then audit coverage of the top tiers."""
    from jobfinder.services import coverage

    out = await coverage.score_all(db)
    await db.commit()  # the boundary: every company's score
    if not p.no_audit:
        await ledger.add(db, "audit_coverage", dedupe=True)
    return out
