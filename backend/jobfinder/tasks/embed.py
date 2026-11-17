"""The `embed` queue's handlers (#126): keeping the retrieval index current, no Claude. worker-embed imports
this module (`python -m jobfinder.tasks embed`), so CPU-bound embedding never holds up io or ai deliveries.

    index_jobs     embed a few batches of stale postings, committing per batch; chains itself while
                   more remain (rerun=True, so one follow-up waits behind the running run)
    index_profile  one user's current profile version (task.user_id)
    embed_sweep    the periodic check (scheduler.cron): queues the two above where they're due, and
                   drops the vectors of postings that went inactive

The model is the process-wide one from integrations.embed.get_embedder(), loaded on the first task that
needs it and kept for the life of the worker (it holds no connections, so there is nothing to close
on WORKER_SHUTDOWN). Vectors are stored under EMBED_MODEL; a loaded model by another name is refused.
"""
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.core.config import settings
from jobfinder.integrations import embed
from jobfinder.models import Task
from jobfinder.services.matching import index
from jobfinder.tasks import ledger
from jobfinder.tasks.runtime import ledger_task

JOB_BATCHES_PER_RUN = 8  # index_jobs embeds up to EMBED_BATCH * this per run, then chains


@ledger_task("index_jobs", ai=False, deadline=timedelta(minutes=15))
async def index_jobs(db: AsyncSession, ai, task: Task, _: None) -> dict:
    """Commits after every batch (the checkpoint), so a deadline or crash keeps the batches done."""
    batch = max(1, settings.embed_batch)
    summary = await index.index_jobs(db, embed.get_embedder(), model=settings.embed_model,
                                     limit=batch * JOB_BATCHES_PER_RUN, batch=batch, checkpoint=db.commit)
    if summary["more"] and summary["embedded"]:  # embedded nothing: chaining would only spin
        summary["next"] = (await ledger.add(db, "index_jobs", rerun=True)).id
    return summary


@ledger_task("index_profile", ai=False, deadline=timedelta(minutes=5))
async def index_profile(db: AsyncSession, ai, task: Task, _: None) -> dict:
    if task.user_id is None:
        raise ValueError("index_profile needs a user")
    return await index.index_profile(db, embed.get_embedder(), task.user_id, model=settings.embed_model)


@ledger_task("embed_sweep", ai=False, deadline=timedelta(minutes=5))
async def embed_sweep(db: AsyncSession, ai, task: Task, _: None) -> dict:
    return await index.sweep(db, model=settings.embed_model)
