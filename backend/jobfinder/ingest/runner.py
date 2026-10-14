"""Top-level ingest run shared by the CLI and the worker handler."""
import logging
from datetime import UTC, datetime, timedelta

from jobfinder.auth import ratelimit
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


async def ingest_usajobs(session_factory: SessionFactory, transport=None) -> IngestStats:
    import httpx

    from jobfinder.ingest.sources.usajobs import SOURCE, build_locations, fetch_usajobs

    stats = IngestStats(source=SOURCE, company="(usajobs.gov)")
    try:
        async with session_factory() as db:
            locations = await build_locations(db)
        async with httpx.AsyncClient(timeout=60, transport=transport) as client:  # no DB session held while crawling
            postings = await fetch_usajobs(client, locations)
        stats.fetched = len(postings)
        async with session_factory() as db:
            if postings:
                stats.new, stats.updated = await service.upsert_postings(db, postings, None)
                # only a successful non-empty fetch may expire jobs, so an API outage never wipes them
                stats.deactivated = await service.expire_stale(db, SOURCE, service.USAJOBS_EXPIRE_AFTER)
    except Exception as e:
        log.exception("usajobs ingest failed")
        stats.error = f"{type(e).__name__}: {e}"[:300]
    return stats


EXTRA_KIND = "ingest_extra"  # rate_events kind: when jobspy/usajobs last ran


async def _extra_due(session_factory: SessionFactory, source: str) -> bool:
    """On the adaptive schedule the run fires every few minutes, but jobspy and usajobs keep their
    own `ingest_interval_hours` cadence: run only if the last run is older than that, and record
    this one. (rate_events are pruned after a day, so intervals over 24h behave as 24h.)"""
    async with session_factory() as db:
        last = await ratelimit.last_at(db, EXTRA_KIND, source)
        if last and last > datetime.now(UTC) - timedelta(hours=get_settings().ingest_interval_hours):
            return False
        await ratelimit.hit(db, EXTRA_KIND, source)
    return True


async def run_ingest(
    session_factory: SessionFactory, *, ats: str | None = None, slug: str | None = None, jobspy: bool | None = None,
    all_boards: bool = False,
) -> dict:
    """The scheduled run fetches only due boards; `all_boards` (the CLI) is a full run, as before."""
    results = await service.ingest_boards(session_factory, ats=ats, slug=slug, all_boards=all_boards)
    # an explicit request (CLI, --jobspy, ats=...) always runs; the scheduled run is gated
    gated = get_settings().adaptive_schedule_enabled and ats is None and not all_boards
    use_jobspy = get_settings().enable_jobspy if jobspy is None else jobspy
    if use_jobspy and not slug and ats in (None, "jobspy") and (
        not (gated and jobspy is None) or await _extra_due(session_factory, "jobspy")
    ):
        results.append(await ingest_jobspy(session_factory))
    if get_settings().usajobs_enabled and not slug and ats in (None, "usajobs") and (
        not gated or await _extra_due(session_factory, "usajobs")
    ):
        results.append(await ingest_usajobs(session_factory))
    return {
        "companies": len(results),
        "fetched": sum(r.fetched for r in results),
        "new": sum(r.new for r in results),
        "updated": sum(r.updated for r in results),
        "deactivated": sum(r.deactivated for r in results),
        "errors": {r.company: r.error for r in results if r.error},
        "detail": [r.__dict__ for r in results],
    }
