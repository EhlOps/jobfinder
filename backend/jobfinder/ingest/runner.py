"""Top-level ingest run shared by the CLI and the worker handler.

A boundary: every session opened here commits its own work (each board in ingest_boards, the jobspy and
usajobs runs here, the extra-source cadence in _extra_due)."""
import logging
from datetime import UTC, datetime, timedelta

import httpx

from jobfinder.core.config import settings
from jobfinder.integrations import jobspy as jobspy_source
from jobfinder.integrations import usajobs
from jobfinder.services import ingestion as service
from jobfinder.services import ratelimit
from jobfinder.services.ingestion import IngestStats, SessionFactory

log = logging.getLogger("jobfinder.ingest")


async def ingest_jobspy(session_factory: SessionFactory) -> IngestStats:
    stats = IngestStats(source=jobspy_source.SOURCE, company="(linkedin/indeed)")
    try:
        async with session_factory() as db:
            queries = await jobspy_source.build_queries(db)
            if not queries:
                stats.error = "no target roles in any profile yet"
                return stats
            postings = await jobspy_source.fetch_jobspy(queries)
            stats.fetched = len(postings)
            if postings:
                stats.new, stats.updated = await service.upsert_postings(db, postings, None)
                await db.commit()  # kept even if the expiry below fails
            stats.deactivated = await service.expire_stale(db, jobspy_source.SOURCE, service.JOBSPY_EXPIRE_AFTER)
            await db.commit()
    except Exception as e:
        log.exception("jobspy ingest failed")
        stats.error = f"{type(e).__name__}: {e}"[:300]
    return stats


async def ingest_usajobs(session_factory: SessionFactory, transport=None) -> IngestStats:
    stats = IngestStats(source=usajobs.SOURCE, company="(usajobs.gov)")
    try:
        async with session_factory() as db:
            locations = await usajobs.build_locations(db)
        async with httpx.AsyncClient(timeout=60, transport=transport) as client:  # no DB session held while crawling
            postings = await usajobs.fetch_usajobs(client, locations)
        stats.fetched = len(postings)
        async with session_factory() as db:
            if postings:
                stats.new, stats.updated = await service.upsert_postings(db, postings, None)
                await db.commit()  # kept even if the expiry below fails
                # only a successful non-empty fetch may expire jobs, so an API outage never wipes them
                stats.deactivated = await service.expire_stale(db, usajobs.SOURCE, service.USAJOBS_EXPIRE_AFTER)
                await db.commit()
    except Exception as e:
        log.exception("usajobs ingest failed")
        stats.error = f"{type(e).__name__}: {e}"[:300]
    return stats


EXTRA_KIND = "ingest_extra"  # rate_events kind: when jobspy/usajobs last ran


async def _extra_due(session_factory: SessionFactory, source: str, *, record: bool = True) -> bool:
    """On the adaptive schedule the run fires every few minutes, but jobspy and usajobs keep their
    own `ingest_interval_hours` cadence: run only if the last run is older than that, and record
    this one (unless `record` is off: the ingest_source task records its own run, #50). (rate_events
    are pruned after a day, so intervals over 24h behave as 24h.)"""
    async with session_factory() as db:
        last = await ratelimit.last_at(db, EXTRA_KIND, source)
        if last and last > datetime.now(UTC) - timedelta(hours=settings.ingest_interval_hours):
            return False
        if record:
            await ratelimit.hit(db, EXTRA_KIND, source)
            await db.commit()
    return True


async def due_extras(
    session_factory: SessionFactory, *, ats: str | None = None, slug: str | None = None, jobspy: bool | None = None,
    all_boards: bool = False, record: bool = True,
) -> list[str]:
    """Which of jobspy/usajobs this run fetches. An explicit request (CLI, --jobspy, ats=...) always
    runs; the scheduled run is gated by their own cadence (_extra_due, which records the run)."""
    gated = settings.adaptive_schedule_enabled and ats is None and not all_boards
    use_jobspy = settings.enable_jobspy if jobspy is None else jobspy
    out = []
    if use_jobspy and not slug and ats in (None, "jobspy") and (
        not (gated and jobspy is None) or await _extra_due(session_factory, "jobspy", record=record)
    ):
        out.append("jobspy")
    if settings.usajobs_enabled and not slug and ats in (None, "usajobs") and (
        not gated or await _extra_due(session_factory, "usajobs", record=record)
    ):
        out.append("usajobs")
    return out


async def ingest_extra(session_factory: SessionFactory, source: str) -> IngestStats:
    """Run one extra source; like ingest_board it records a failure in the stats instead of raising."""
    if source == "jobspy":
        return await ingest_jobspy(session_factory)
    if source == "usajobs":
        return await ingest_usajobs(session_factory)
    raise ValueError(f"unknown ingest source {source!r}")


def summarize(results: list[IngestStats]) -> dict:
    return {
        "companies": len(results),
        "fetched": sum(r.fetched for r in results),
        "new": sum(r.new for r in results),
        "updated": sum(r.updated for r in results),
        "deactivated": sum(r.deactivated for r in results),
        "errors": {r.company: r.error for r in results if r.error},
        "detail": [r.__dict__ for r in results],
    }


async def run_ingest(
    session_factory: SessionFactory, *, ats: str | None = None, slug: str | None = None, jobspy: bool | None = None,
    all_boards: bool = False,
) -> dict:
    """One run, inline: the boards INGEST_CONCURRENCY at a time, then the extras. The scheduled run
    fetches only due boards; `all_boards` (the CLI) is a full run, as before. The ledger's ingest_tick
    fans the same work out as tasks instead (#50)."""
    results = await service.ingest_boards(session_factory, ats=ats, slug=slug, all_boards=all_boards)
    for source in await due_extras(session_factory, ats=ats, slug=slug, jobspy=jobspy, all_boards=all_boards):
        results.append(await ingest_extra(session_factory, source))
    return summarize(results)
