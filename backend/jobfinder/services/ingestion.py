"""Ingestion: fetch boards, upsert into the `jobs` cache, expire jobs that disappeared, and the top-level
run the CLI and the task handlers share.

Ingestion's commits all live in this file (#53): `ingest_board` opens one session per board and is
the boundary for each, so one board failing never loses another's run; the jobspy and usajobs runs and the
extra-source cadence (`_extra_due`) commit their own sessions too. Everything else runs in the caller's
session and the caller commits (the task handler, the CLI).
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial

import httpx

from jobfinder.core.config import settings
from jobfinder.core.db import AsyncSession
from jobfinder.core.http import new_client
from jobfinder.integrations import jobspy as jobspy_source
from jobfinder.integrations import usajobs
from jobfinder.integrations.ats.registry import ATS_FETCHERS, Fetcher
from jobfinder.integrations.recipes import RecipeUnavailable, run_recipe
from jobfinder.integrations.recipes.health import needs_reinduce, record_run
from jobfinder.models import Company
from jobfinder.models.posting import JobPosting
from jobfinder.repositories import boards as boards_repo
from jobfinder.repositories import postings as postings_repo
from jobfinder.services import ingest_schedule as schedule
from jobfinder.services import ratelimit
from jobfinder.services.board_selection import Target, select_boards
from jobfinder.tasks import ledger

log = logging.getLogger("jobfinder.ingest")

VALID_SIZES = {"startup", "mid", "large"}
# An empty response from a board that had more than this many active jobs is treated as an
# API glitch: keep the jobs rather than deactivating the whole board.
EMPTY_GUARD_MIN_ACTIVE = 5
# ...but after this many consecutive empty runs the closure is real: deactivate and carry on.
EMPTY_CONFIRM_RUNS = 3
EMPTY_ERROR_PREFIX = "empty response"
JOBSPY_EXPIRE_AFTER = timedelta(days=14)
USAJOBS_EXPIRE_AFTER = timedelta(days=14)

SessionFactory = Callable[[], AsyncSession]


@dataclass
class IngestStats:
    source: str
    company: str = ""
    fetched: int = 0
    new: int = 0
    updated: int = 0
    deactivated: int = 0
    error: str | None = None


# ── upsert / expire ───────────────────────────────────────────────────────
async def upsert_postings(db: AsyncSession, postings: list[JobPosting], company_id: int | None) -> tuple[int, int]:
    """Insert new postings and refresh existing ones. Returns (new, updated)."""
    return await postings_repo.upsert(db, postings, company_id)


async def deactivate_missing(db: AsyncSession, source: str, company_id: int, seen_ids: list[str]) -> int:
    return await postings_repo.deactivate_missing(db, source, company_id, seen_ids)


async def expire_stale(db: AsyncSession, source: str, older_than: timedelta) -> int:
    """For sources without per-company boards (jobspy, usajobs): expire jobs not seen recently."""
    return await postings_repo.expire_stale(db, source, older_than)


# ── fetching ──────────────────────────────────────────────────────────────
async def _fetch_with_retry(
    fetcher: Callable[..., Awaitable[list[JobPosting]]], client: httpx.AsyncClient, slug: str, name: str
) -> list[JobPosting]:
    try:
        return await fetcher(client, slug, name)
    except (httpx.TransportError, httpx.HTTPStatusError) as first:
        if isinstance(first, httpx.HTTPStatusError) and first.response.status_code < 500:
            raise  # 404 etc. won't fix itself
        await asyncio.sleep(1)
        return await fetcher(client, slug, name)


def _describe(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        return "board not found (check the slug)" if code == 404 else f"HTTP {code}"
    if isinstance(e, httpx.TimeoutException):
        return "timed out"
    return f"{type(e).__name__}: {e}"[:300]


def _record_failure(company: Company, error: str) -> bool:
    """Count a failed run; disable the board (with the reason) once it has failed N times in a row.

    Returns True when this failure auto-disabled the board."""
    company.last_error = error
    company.consecutive_failures = (company.consecutive_failures or 0) + 1
    limit = settings.ingest_max_consecutive_failures
    if company.enabled and limit > 0 and company.consecutive_failures >= limit:
        company.enabled = False
        company.disabled_reason = f"auto-disabled after {company.consecutive_failures} consecutive failures: {error}"[:500]
        return True
    return False


async def _schedule_next(db: AsyncSession, company: Company, *, failed: bool = False) -> None:
    """Set the board's adaptive interval and next slot (services.ingest_schedule); no-op when that's off."""
    s = settings
    if not s.adaptive_schedule_enabled:
        return
    at = schedule.now()
    first = await postings_repo.first_seen(db, company.id)
    since = at - schedule.CHURN_WINDOW
    new_jobs = 0
    if first is not None:  # the board's first batch is its backlog, not churn
        new_jobs = await postings_repo.count_new_since(db, company.id, max(since, first + schedule.FIRST_BATCH))
    if first is None or first > since:  # younger than the window: at least the base cadence
        new_jobs = max(new_jobs, s.ingest_churn_pivot)
    interval = schedule.board_interval(
        new_jobs, schedule.effective_tier(company.coveted_tier, company.origin),
        base=s.ingest_base_interval_minutes, min_minutes=s.ingest_min_interval_minutes,
        max_minutes=s.ingest_max_interval_minutes,
    )
    delay = interval
    if failed:
        failures = company.consecutive_failures or 0
        delay = schedule.failure_delay(interval, failures, s.ingest_max_interval_minutes)
    company.fetch_interval_minutes = interval
    company.next_fetch_at = schedule.next_fetch(company.id, delay, at, s.ingest_max_interval_minutes)


async def _fail_board(db: AsyncSession, company: Company, error: str, now: datetime) -> None:
    """Record a failed attempt. A board that just got auto-disabled is never polled again,
    so its jobs would stay active with dead apply links: deactivate them now."""
    disabled = _record_failure(company, error)
    company.last_fetched_at = now  # an attempt counts, so selection rotates past dead boards
    await _schedule_next(db, company, failed=True)  # and its next slot backs off exponentially
    if disabled:
        await postings_repo.deactivate_board(db, company.ats, company.id)


def _empty_streak(company: Company) -> int:
    """Consecutive empty runs so far, read back from last_error ("empty response; ... (run k/N)")."""
    m = re.fullmatch(rf"{EMPTY_ERROR_PREFIX}; .*\(run (\d+)/\d+\)", company.last_error or "")
    return int(m.group(1)) if m else 0


def _record_success(company: Company, now: datetime) -> None:
    company.last_error = None
    company.consecutive_failures = 0
    company.last_success_at = now
    if company.enabled:
        company.disabled_reason = None


def board_fetcher(company: Company) -> Fetcher | None:
    """The fetcher for a board: its ATS adapter, or the recipe runner for ats='recipe' boards."""
    if company.ats == "recipe":
        return partial(run_recipe, recipe=company.recipe)
    return ATS_FETCHERS.get(company.ats)


def _recipe_run(company: Company, n_postings: int, error: str | None = None) -> bool:
    """Recipe boards only: fold the run into the recipe's health (saved with the run's other changes).
    True when the recipe is now degraded and due a re-induction (see _queue_reinduce)."""
    if company.ats != "recipe":
        return False
    record_run(company, n_postings, error)
    return needs_reinduce(company)


async def _queue_reinduce(db: AsyncSession, company_id: int) -> None:
    """Queue the LLM self-heal for a degraded recipe board, once the run is recorded on the session."""
    if not settings.resolver_llm_enabled:
        return
    try:
        # a savepoint, so a failed add never loses the run's changes; the task commits with the run's boundary
        async with db.begin_nested():
            await ledger.add(db, "reinduce_recipe", {"company_id": company_id}, dedupe=True)
    except Exception:  # the run itself is recorded; the next degraded run queues it again
        log.exception("could not queue re-induction for board %s", company_id)


async def ingest_company(db: AsyncSession, client: httpx.AsyncClient, company: Company) -> IngestStats:
    stats = IngestStats(source=company.ats, company=company.name)
    fetcher = board_fetcher(company)
    if fetcher is None:  # e.g. an ATS removed from the registry: record it on the board, don't crash the run
        stats.error = f"unknown ATS '{company.ats}'"
        await _fail_board(db, company, stats.error, datetime.now(UTC))
        return stats
    try:
        postings = await _fetch_with_retry(fetcher, client, company.slug, company.name)
    except RecipeUnavailable as e:  # e.g. the renderer is off: not the board's fault, so not a failure
        stats.error = f"skipped: {e}"
        return stats
    except Exception as e:
        stats.error = _describe(e)
        heal = _recipe_run(company, 0, stats.error)
        await _fail_board(db, company, stats.error, datetime.now(UTC))
        if heal:
            await _queue_reinduce(db, company.id)
        return stats

    stats.fetched = len(postings)
    if postings:
        stats.new, stats.updated = await upsert_postings(db, postings, company.id)
        stats.deactivated = await deactivate_missing(db, company.ats, company.id, [p.external_id for p in postings])
    else:
        active = await postings_repo.count_active(db, company.ats, company.id)
        streak = _empty_streak(company) + 1
        if active and active > EMPTY_GUARD_MIN_ACTIVE and streak < EMPTY_CONFIRM_RUNS:
            # suspicious: keep the jobs. Not a failure (consecutive_failures untouched), so it never
            # disables the board; the streak lives in last_error until it is confirmed or a fetch succeeds.
            stats.error = f"{EMPTY_ERROR_PREFIX}; kept {active} active jobs (run {streak}/{EMPTY_CONFIRM_RUNS})"
            company.last_error = stats.error
            company.last_fetched_at = datetime.now(UTC)
            heal = _recipe_run(company, 0)
            await _schedule_next(db, company)
            if heal:
                await _queue_reinduce(db, company.id)
            return stats
        stats.deactivated = await deactivate_missing(db, company.ats, company.id, [])
    now = datetime.now(UTC)
    _record_success(company, now)
    company.last_fetched_at = now
    heal = _recipe_run(company, len(postings))
    await _schedule_next(db, company)
    if heal:
        await _queue_reinduce(db, company.id)
    return stats


async def due_board_ids(
    db: AsyncSession, *, ats: str | None = None, slug: str | None = None, all_boards: bool = False,
) -> list[int]:
    """The boards a run fetches: every enabled one, or with the adaptive schedule only those that are due
    (an explicit `ats`/`slug` or `all_boards` always fetches), the discovered ones capped by selection."""
    due = settings.adaptive_schedule_enabled and not (ats or slug or all_boards)
    boards = await boards_repo.enabled_boards(db, ats=ats, slug=slug, due_by=schedule.now() if due else None)
    if not slug:  # an explicit board is always fetched; otherwise cap the discovered ones
        statuses = await boards_repo.activated_statuses(db)
        boards = select_boards(
            boards, [Target.from_status(s) for s in statuses], settings.ingest_max_discovered_boards_per_run
        )
    return [b.id for b in boards]


def http_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """The client a board fetch uses; worker-io passes its shared transport (tasks.io)."""
    return new_client(timeout_s=30, follow_redirects=True, transport=transport)


async def ingest_board(session_factory: SessionFactory, client: httpx.AsyncClient, company_id: int) -> IngestStats:
    """Fetch one board in, and commit it with, its own session: the per-board boundary. Never raises
    (but lets a cancellation through): a crash is recorded on the board and returned as the error."""
    async with session_factory() as db:
        # plain values, captured up front: after a rollback the ORM object is expired and
        # touching it in async code raises (MissingGreenlet / PendingRollbackError)
        name, board_ats = "?", "?"
        try:
            company = await db.get(Company, company_id)
            if company is None or not company.enabled:  # removed or disabled since it was picked (#50)
                return IngestStats(source="?", company=f"board {company_id}", error="skipped: board gone or disabled")
            name, board_ats = company.name, company.ats
            stats = await ingest_company(db, client, company)
            await db.commit()
            return stats
        except Exception as e:  # last line of defence: never lose the whole run
            log.exception("ingest failed for %s", name)
            error = _describe(e)
            try:  # still count it toward the board's health
                await db.rollback()
                company = await db.get(Company, company_id)
                await _fail_board(db, company, error, datetime.now(UTC))
                await db.commit()
            except Exception:
                log.exception("could not record failure for %s", name)
            return IngestStats(source=board_ats, company=name, error=error)


async def ingest_boards(
    session_factory: SessionFactory, *, ats: str | None = None, slug: str | None = None,
    concurrency: int | None = None, all_boards: bool = False,
) -> list[IngestStats]:
    """Poll the boards `due_board_ids` picks, INGEST_CONCURRENCY at a time. One company failing never
    affects the others: each board is fetched in, and committed by, its own session (ingest_board).
    The CLI runs this; the ledger's ingest_tick fans out one ingest_board task per board instead (#50)."""
    async with session_factory() as db:
        ids = await due_board_ids(db, ats=ats, slug=slug, all_boards=all_boards)

    sem = asyncio.Semaphore(concurrency or settings.ingest_concurrency)

    async with http_client() as client:
        async def one(company_id: int) -> IngestStats:
            async with sem:
                return await ingest_board(session_factory, client, company_id)

        results = await asyncio.gather(*(one(i) for i in ids), return_exceptions=True)
        out: list[IngestStats] = []
        for company_id, r in zip(ids, results, strict=True):
            if isinstance(r, BaseException):  # ingest_board swallows Exception; this catches e.g. cancellation
                log.error("board task crashed", exc_info=r)
                r = IngestStats(source="?", company=f"board {company_id}", error=_describe(r) if isinstance(r, Exception) else type(r).__name__)
            out.append(r)
        return out


# ---------------------------------------------------------------- the run (was ingest/runner.py, #53)


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
                stats.new, stats.updated = await upsert_postings(db, postings, None)
                await db.commit()  # kept even if the expiry below fails
            stats.deactivated = await expire_stale(db, jobspy_source.SOURCE, JOBSPY_EXPIRE_AFTER)
            await db.commit()
    except Exception as e:
        log.exception("jobspy ingest failed")
        stats.error = f"{type(e).__name__}: {e}"[:300]
    return stats


async def ingest_usajobs(session_factory: SessionFactory) -> IngestStats:
    stats = IngestStats(source=usajobs.SOURCE, company="(usajobs.gov)")
    try:
        async with session_factory() as db:
            locations = await usajobs.build_locations(db)
        async with new_client(timeout_s=60) as client:  # no DB session held while crawling
            postings = await usajobs.fetch_usajobs(client, locations)
        stats.fetched = len(postings)
        async with session_factory() as db:
            if postings:
                stats.new, stats.updated = await upsert_postings(db, postings, None)
                await db.commit()  # kept even if the expiry below fails
                # only a successful non-empty fetch may expire jobs, so an API outage never wipes them
                stats.deactivated = await expire_stale(db, usajobs.SOURCE, USAJOBS_EXPIRE_AFTER)
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
    results = await ingest_boards(session_factory, ats=ats, slug=slug, all_boards=all_boards)
    for source in await due_extras(session_factory, ats=ats, slug=slug, jobspy=jobspy, all_boards=all_boards):
        results.append(await ingest_extra(session_factory, source))
    return summarize(results)
