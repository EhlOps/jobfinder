"""The `io` queue's handlers: fetching and planning, no Claude. worker-io imports this module
(`python -m jobfinder.tasks io`), so a long ingest run never holds up an `ai` delivery: the two queues
are separate streams consumed by separate processes.

Handlers are `(db, ai, task, payload) -> dict` like tasks.ai's; `ai` is unused here.
"""
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import Task
from jobfinder.tasks import ledger
from jobfinder.tasks.brokers import io_broker
from jobfinder.tasks.runtime import ledger_task


class ScoreCovetedArgs(BaseModel):
    no_audit: bool = False  # the admin's "rescore only" skips the coverage audit that follows


@ledger_task(io_broker, "ingest_jobs", ai=False)
async def ingest_jobs(db: AsyncSession, ai, task: Task, _: None) -> dict:
    """Poll all ATS boards (and jobspy/usajobs when enabled). Each board commits in its own session
    (services.ingestion.ingest_boards), so this task's session holds only the follow-up."""
    from jobfinder.core.config import settings
    from jobfinder.db import SessionLocal
    from jobfinder.ingest.runner import run_ingest

    summary = await run_ingest(SessionLocal)
    # Matching is planned, not fanned out: the planner picks who needs it most and staggers the runs.
    # Adaptive ticks come every few minutes and often fetch nothing: plan only when jobs changed.
    if not settings.adaptive_schedule_enabled or summary.get("new") or summary.get("deactivated"):
        await ledger.add(db, "plan_work", rerun=True)  # a running plan missed these jobs
    return summary


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
