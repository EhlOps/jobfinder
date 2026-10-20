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


class ResolveLinkArgs(BaseModel):
    link_id: int


class AuditCoverageArgs(BaseModel):
    company_id: int | None = None  # audit just this employer's targets
    target_id: int | None = None  # or just this one


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


# ---------------------------------------------------------------- resolver (deterministic rungs, #50)


@ledger_task(io_broker, "resolve_job_link", payload=ResolveLinkArgs, ai=False)
async def resolve_job_link(db: AsyncSession, ai, task: Task, p: ResolveLinkArgs) -> dict:
    """A pasted job link whose board is new: add it, fetch it now, then let the planner match the new jobs.
    When no deterministic rung reads the site, induce_recipe asks Claude on the ai queue (#50); the link
    stays 'resolving' until that finishes it."""
    from jobfinder.services.resolver import llm_allowed, resolve_link

    out = await resolve_link(db, p.link_id, allow_llm=await llm_allowed(db))
    induce = out.pop("induce", None)
    if induce is not None:
        await ledger.add(db, "induce_recipe", {"link_id": p.link_id, **induce}, user_id=task.user_id, dedupe=True)
        out["queued"] = "induce_recipe"
    await db.commit()  # the boundary: the link's outcome, the board and jobs it found, or its induce_recipe
    if out.get("new"):
        await ledger.add(db, "plan_work", dedupe=True)
    return out


@ledger_task(io_broker, "audit_coverage", payload=AuditCoverageArgs, ai=False)
async def audit_coverage(db: AsyncSession, ai, task: Task, p: AuditCoverageArgs) -> dict:
    """Send coveted employers with no working board through the resolver (payload may name just one).
    Deterministic rungs only: services.coverage never hands the resolver Claude (#50)."""
    from jobfinder.services import coverage

    out = await coverage.audit_coverage(
        db, company_id=p.company_id, target_id=p.target_id,
        checkpoint=db.commit,  # keep each target's outcome as the audit goes
    )
    await db.commit()
    if out.get("resolved"):
        await ledger.add(db, "plan_work", dedupe=True)
    return out
