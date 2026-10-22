"""The `io` queue's handlers: fetching and planning, no Claude. worker-io imports this module
(`python -m jobfinder.tasks io`), so a long ingest run never holds up an `ai` delivery: the two queues
are separate streams consumed by separate processes.

Handlers are `(db, ai, task, payload) -> dict` like tasks.ai's, registered with @ledger_task(kind) on
the io broker (tasks.kinds); `ai` is unused here.
"""
from datetime import timedelta
from typing import Literal

import httpx
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from taskiq import TaskiqEvents, TaskiqState

import jobfinder.tasks.documents  # noqa: F401  (registers parse_document and fetch_link, on priority since #124)
from jobfinder.core.context import default_context
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


PLAN_COALESCE = timedelta(minutes=1)  # boards finishing within this of each other share one plan_work


_http: httpx.AsyncBaseTransport | None = None  # worker-io's connection pool for board fetches


def shared_http() -> httpx.AsyncBaseTransport:
    """The process's board-fetch transport, created on first use: the boards fetched in parallel share its
    connection pool instead of opening new connections per ingest_board. Each task still gets its own
    client on it, so one board's cookies never reach another's."""
    global _http
    if _http is None:
        _http = httpx.AsyncHTTPTransport()
    return _http


@io_broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def close_http(state: TaskiqState | None = None) -> None:
    global _http
    if _http is not None:
        transport, _http = _http, None
        await transport.aclose()


class IngestBoardArgs(BaseModel):
    company_id: int


class IngestSourceArgs(BaseModel):
    source: Literal["jobspy", "usajobs"]


@ledger_task("ingest_tick", ai=False)
async def ingest_tick(db: AsyncSession, ai, task: Task, _: None) -> dict:
    """The scheduled ingest (#50): pick the due boards (the adaptive cadence and the discovered-board cap)
    and queue one ingest_board per board, plus one ingest_source per due extra source, all committed
    with this task. Each board then runs, retries and logs on its own, worker-io's slots in parallel,
    so a slow or failing board holds up nothing else. Deduped: a board still queued or running from an
    earlier tick isn't queued twice."""
    from jobfinder.services import ingestion

    ids = await ingestion.due_board_ids(db)
    for company_id in ids:
        await ledger.add(db, "ingest_board", {"company_id": company_id}, dedupe=True)
    # not recorded as run here: the ingest_source task records its run, so a tick that rolls back
    # doesn't skip the source for a whole interval
    sources = await ingestion.due_extras(default_context().sessions, record=False)
    for source in sources:
        await ledger.add(db, "ingest_source", {"source": source}, dedupe=True)
    return {"due": len(ids), "sources": sources}


@ledger_task("ingest_board", payload=IngestBoardArgs, ai=False)
async def ingest_board(db: AsyncSession, ai, task: Task, p: IngestBoardArgs) -> dict:
    """Fetch one board. The board commits in its own session (services.ingestion.ingest_board), which
    records a failed fetch on the board (its backoff is the board's retry); this task's session holds
    only the follow-up."""
    from jobfinder.services import ingestion

    # Not closed: that would close the shared transport. The client holds only this board's cookies.
    client = ingestion.http_client(shared_http())
    stats = await ingestion.ingest_board(default_context().sessions, client, p.company_id)
    return await _changed(db, stats)


@ledger_task("ingest_source", payload=IngestSourceArgs, ai=False)
async def ingest_source(db: AsyncSession, ai, task: Task, p: IngestSourceArgs) -> dict:
    """Fetch jobspy or usajobs (the sources without boards), on their own so a long scrape never holds
    a board up. Like ingest_board, it commits in its own sessions (services.ingestion)."""
    from jobfinder.services import ingestion, ratelimit

    await ratelimit.hit(db, ingestion.EXTRA_KIND, p.source)  # its cadence (ingestion._extra_due), kept with the result
    return await _changed(db, await ingestion.ingest_extra(default_context().sessions, p.source))


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


@ledger_task("plan_work", ai=False)
async def plan_work(db: AsyncSession, ai, task: Task, _: None) -> dict:
    from jobfinder.services import planner

    return await planner.plan(db)


@ledger_task("daily_emails", ai=False)
async def daily_emails(db: AsyncSession, ai, task: Task, _: None) -> dict:
    """Hourly tick: queue question collection on `ai` for users whose digest is coming up, then send the
    daily email to every user whose chosen hour has come, with the questions collected so far (#50)."""
    from jobfinder.core.clock import utcnow
    from jobfinder.services import digest

    now = utcnow()
    queued = await digest.schedule_questions(db, now)
    await db.commit()  # the boundary: the collection rows, before the per-user sends commit their own
    return {**await digest.send_due(db, now), "questions_queued": queued}


@ledger_task("discover_boards", ai=False)
async def discover_boards(db: AsyncSession, ai, task: Task, _: None) -> dict:
    from jobfinder.services import discovery

    return (await discovery.discover_boards(db, checkpoint=db.commit)).as_dict()  # keep each board as it's added


@ledger_task("score_coveted", payload=ScoreCovetedArgs, ai=False)
async def score_coveted(db: AsyncSession, ai, task: Task, p: ScoreCovetedArgs) -> dict:
    """Weekly: recompute how sought-after each employer is, then audit coverage of the top tiers."""
    from jobfinder.services import coverage

    out = await coverage.score_all(db)
    await db.commit()  # the boundary: every company's score
    if not p.no_audit:
        await ledger.add(db, "audit_coverage", dedupe=True)
    return out


# ---------------------------------------------------------------- resolver (deterministic rungs, #50)


@ledger_task("resolve_job_link", payload=ResolveLinkArgs, ai=False)
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
        await _plan(db)
    return out


@ledger_task("audit_coverage", payload=AuditCoverageArgs, ai=False)
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
        await _plan(db)
    return out
