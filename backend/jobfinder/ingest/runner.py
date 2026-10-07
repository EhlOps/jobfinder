"""Top-level ingest run shared by the CLI and the worker handler."""
import logging

from jobfinder.config import get_settings
from jobfinder.ingest import service
from jobfinder.ingest.service import IngestStats, SessionFactory

log = logging.getLogger("jobfinder.ingest")


async def ingest_jobspy(session_factory: SessionFactory) -> IngestStats:
    from jobfinder.ingest.sources.jobspy_source import SOURCE, build_queries, fetch_jobspy

    stats = IngestStats(source=SOURCE, company="(linkedin/indeed)")
    try:
        async with session_factory() as db:
            queries = await build_queries(db)
            if not queries:
                stats.error = "no target roles in any profile yet"
                return stats
            postings = await fetch_jobspy(queries)
            stats.fetched = len(postings)
            if postings:
                stats.new, stats.updated = await service.upsert_postings(db, postings, None)
            stats.deactivated = await service.expire_stale(db, SOURCE, service.JOBSPY_EXPIRE_AFTER)
    except Exception as e:
        log.exception("jobspy ingest failed")
        stats.error = f"{type(e).__name__}: {e}"[:300]
    return stats


async def run_ingest(
    session_factory: SessionFactory, *, ats: str | None = None, slug: str | None = None, jobspy: bool | None = None
) -> dict:
    results = await service.ingest_boards(session_factory, ats=ats, slug=slug)
    use_jobspy = get_settings().enable_jobspy if jobspy is None else jobspy
    if use_jobspy and not slug and ats in (None, "jobspy"):
        results.append(await ingest_jobspy(session_factory))
    return {
        "companies": len(results),
        "fetched": sum(r.fetched for r in results),
        "new": sum(r.new for r in results),
        "updated": sum(r.updated for r in results),
        "deactivated": sum(r.deactivated for r in results),
        "errors": {r.company: r.error for r in results if r.error},
        "detail": [r.__dict__ for r in results],
    }
