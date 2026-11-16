"""Indexing for embedding retrieval (#126): vectors for postings and profile versions, kept current.

    index_jobs     embed active postings with no vector from the model, or whose text changed since
                   (repositories.embeddings.SOURCE_HASH), newest first, committing per batch
    index_profile  embed the current profile version's queries (embed_text.profile_queries), snapshot
                   the version (profile_versions) and drop the vectors of older versions
    sweep          what the periodic embed_sweep queues: index_jobs if any posting needs it, and
                   index_profile per user whose current version lacks vectors
    reindex        `jobfinder ops reindex`: drop a model's vectors and sweep, to rebuild them

The embedding itself is CPU-bound and synchronous (the Embedder), so it runs on a thread, off the event
loop the worker's heartbeat runs on. Nothing here commits: index_jobs takes a `checkpoint` callable
(the task passes the session's commit), the rest leave it to their task or command.
"""
import asyncio
from collections.abc import Awaitable, Callable

from jobfinder.core.db import AsyncSession
from jobfinder.integrations.embed import Embedder
from jobfinder.repositories import embeddings as repo
from jobfinder.repositories import profiles as profiles_repo
from jobfinder.services.matching.embed_text import job_doc, profile_queries
from jobfinder.tasks import ledger

Checkpoint = Callable[[], Awaitable[None]]


async def _nothing() -> None:
    return None


def _check(embedder: Embedder, model: str) -> None:
    """Vectors are stored under `model`: refuse to write another model's vectors under its name."""
    if embedder.model != model:
        raise ValueError(f"EMBED_MODEL is {model!r} but the loaded model is {embedder.model!r}")


def _queries(status: dict, background: dict) -> list[str]:
    return profile_queries({"status": status or {}, "background": background or {}})


async def index_jobs(db: AsyncSession, embedder: Embedder, *, model: str, limit: int, batch: int,
                     checkpoint: Checkpoint | None = None) -> dict:
    """Embed up to `limit` stale postings, `batch` per embedding call, and `checkpoint` after each batch,
    so a crash keeps what was embedded. `more` says whether stale postings remain (the task chains)."""
    _check(embedder, model)
    checkpoint = checkpoint or _nothing
    batch = max(1, batch)
    embedded, drained = 0, False
    while embedded < limit:
        rows = await repo.stale_jobs(db, model, min(batch, limit - embedded))
        if not rows:
            drained = True
            break
        vectors = await asyncio.to_thread(embedder.embed_documents, [job_doc(r) for r in rows])
        await repo.upsert_job_embeddings(db, model, [(r["id"], r["source_hash"], v) for r, v in zip(rows, vectors, strict=True)])
        await checkpoint()
        embedded += len(rows)
    more = False if drained else await repo.any_stale_jobs(db, model)
    return {"model": model, "embedded": embedded, "more": more}


async def index_profile(db: AsyncSession, embedder: Embedder, user_id: int, *, model: str) -> dict:
    """Bring one user's query vectors up to their current profile version. A profile with nothing to
    query by (no target roles, no summary) gets its snapshot but no vectors."""
    _check(embedder, model)
    profile = await profiles_repo.get(db, user_id)
    if profile is None:
        return {"skipped": "no profile"}
    version, status, background = profile.version, profile.status or {}, profile.background or {}
    facts = [{"question": f.question, "answer": f.answer}
             for f in await profiles_repo.facts(db, user_id) if (f.answer or "").strip()]
    await repo.save_profile_version(db, user_id, version, status, background, facts)
    stored = 0
    if not await repo.has_profile_vectors(db, user_id, model, version):
        queries = _queries(status, background)
        if queries:
            vectors = await asyncio.to_thread(embedder.embed_queries, queries)
            await repo.insert_profile_embeddings(
                db, user_id, model, version, [(i, q, v) for i, (q, v) in enumerate(zip(queries, vectors, strict=True))])
            stored = len(queries)
    deleted = await repo.delete_older_profile_embeddings(db, user_id, model, version)
    return {"model": model, "version": version, "queries": stored, "deleted": deleted}


async def sweep(db: AsyncSession, *, model: str) -> dict:
    """Queue (deduped) the indexing that's due, and drop the vectors of postings no longer active (kept,
    they would crowd live postings out of the HNSW index's nearest neighbours). Profiles with nothing to
    query by are skipped, or they would be queued again on every sweep."""
    pruned = await repo.delete_inactive_job_embeddings(db, model)
    jobs = await repo.any_stale_jobs(db, model)
    if jobs:
        await ledger.add(db, "index_jobs", dedupe=True)
    users = [uid for uid, status, background in await repo.profiles_without_vectors(db, model)
             if _queries(status, background)]
    for uid in users:
        await ledger.add(db, "index_profile", user_id=uid, dedupe=True)
    return {"index_jobs": jobs, "index_profile": users, "pruned": pruned}


async def reindex(db: AsyncSession, *, model: str) -> dict:
    """Drop `model`'s posting and profile vectors and queue an embed_sweep to rebuild them (after a
    restore, whose dump leaves those tables' data out, or to rebuild an index from scratch)."""
    jobs, profiles = await repo.delete_model(db, model)
    # rerun: a sweep running now read the vectors before this delete, so it would rebuild nothing
    task = await ledger.add(db, "embed_sweep", rerun=True)
    return {"model": model, "job_vectors_deleted": jobs, "profile_vectors_deleted": profiles, "sweep_task": task.id}
