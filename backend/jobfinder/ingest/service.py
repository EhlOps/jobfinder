"""Ingestion: fetch boards, upsert into the `jobs` cache, expire jobs that disappeared."""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial

import httpx
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.config import get_settings
from jobfinder.ingest import schedule
from jobfinder.ingest.base import JobPosting, safe_http_url
from jobfinder.ingest.recipes import RecipeUnavailable
from jobfinder.ingest.selection import Target, select_boards
from jobfinder.ingest.sources import ATS_FETCHERS, Fetcher
from jobfinder.models import Company, Job, Profile, User

log = logging.getLogger("jobfinder.ingest")

VALID_SIZES = {"startup", "mid", "large"}
UPSERT_CHUNK = 200
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
_DETAIL_COLUMNS = (
    "description_text", "salary_min", "salary_max", "salary_currency", "posted_at", "workplace_type",
)


def _clean(s: str | None) -> str:
    return (s or "").replace("\x00", "")  # Postgres text can't hold NUL


def _row(p: JobPosting, company_id: int | None, now: datetime) -> dict:
    return {
        "source": p.source, "external_id": _clean(p.external_id), "company_id": company_id,
        "company_name": _clean(p.company_name)[:200], "title": _clean(p.title)[:500],
        "location": _clean(p.location)[:500], "workplace_type": p.workplace_type,
        "employment_type": (p.employment_type or None) and p.employment_type[:32],
        "seniority": p.seniority, "salary_min": p.salary_min, "salary_max": p.salary_max,
        "salary_currency": p.salary_currency, "description_text": _clean(p.description_text),
        "url": safe_http_url(_clean(p.url))[:1024], "posted_at": p.posted_at, "dedupe_hash": p.dedupe_hash,
        "last_seen_at": now, "is_active": True,
    }


async def upsert_postings(db: AsyncSession, postings: list[JobPosting], company_id: int | None) -> tuple[int, int]:
    """Insert new postings and refresh existing ones. Returns (new, updated)."""
    now = datetime.now(UTC)
    unique = {(p.source, p.external_id): p for p in postings}.values()  # ON CONFLICT can't hit a row twice
    rows = [_row(p, company_id, now) for p in unique]
    new = updated = 0
    for i in range(0, len(rows), UPSERT_CHUNK):
        stmt = insert(Job).values(rows[i : i + UPSERT_CHUNK])
        keep = {c: stmt.excluded[c] for c in rows[0] if c not in ("source", "external_id")}
        # A failed detail fetch yields an empty description (and nothing derived from it); keep the stored values.
        no_desc = stmt.excluded.description_text == ""
        for c in _DETAIL_COLUMNS:
            new_val = sa.func.coalesce(stmt.excluded[c], Job.__table__.c[c]) if c == "posted_at" else stmt.excluded[c]
            keep[c] = sa.case((no_desc, Job.__table__.c[c]), else_=new_val)  # a missing date never erases a known one
        res = await db.execute(
            stmt.on_conflict_do_update(constraint="uq_job_source_external", set_=keep)
            .returning(sa.literal_column("(xmax = 0)").label("inserted"))
        )
        flags = [r.inserted for r in res]
        new += sum(flags)
        updated += len(flags) - sum(flags)
    await db.commit()
    return new, updated


async def deactivate_missing(db: AsyncSession, source: str, company_id: int, seen_ids: list[str]) -> int:
    res = await db.execute(
        sa.update(Job)
        .where(
            Job.source == source, Job.company_id == company_id, Job.is_active.is_(True),
            sa.not_(Job.external_id == sa.any_(sa.bindparam("ids", seen_ids, type_=ARRAY(sa.String)))),
        )
        .values(is_active=False)
    )
    await db.commit()
    return res.rowcount or 0


async def expire_stale(db: AsyncSession, source: str, older_than: timedelta) -> int:
    """For sources without per-company boards (jobspy, usajobs): expire jobs not seen recently."""
    res = await db.execute(
        sa.update(Job)
        .where(Job.source == source, Job.is_active.is_(True), Job.last_seen_at < datetime.now(UTC) - older_than)
        .values(is_active=False)
    )
    await db.commit()
    return res.rowcount or 0


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
    limit = get_settings().ingest_max_consecutive_failures
    if company.enabled and limit > 0 and company.consecutive_failures >= limit:
        company.enabled = False
        company.disabled_reason = f"auto-disabled after {company.consecutive_failures} consecutive failures: {error}"[:500]
        return True
    return False


async def _schedule_next(db: AsyncSession, company: Company, *, failed: bool = False) -> None:
    """Set the board's adaptive interval and next slot (ingest.schedule); no-op when that's off."""
    s = get_settings()
    if not s.adaptive_schedule_enabled:
        return
    at = schedule.now()
    first = await db.scalar(sa.select(sa.func.min(Job.first_seen_at)).where(Job.company_id == company.id))
    since = at - schedule.CHURN_WINDOW
    new_jobs = 0
    if first is not None:  # the board's first batch is its backlog, not churn
        new_jobs = await db.scalar(
            sa.select(sa.func.count()).select_from(Job).where(
                Job.company_id == company.id, Job.first_seen_at > max(since, first + schedule.FIRST_BATCH)
            )
        ) or 0
    if first is None or first > since:  # younger than the window: at least the base cadence
        new_jobs = max(new_jobs, schedule.CHURN_PIVOT)
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
    """Record a failed attempt and commit. A board that just got auto-disabled is never polled again,
    so its jobs would stay active with dead apply links: deactivate them now."""
    disabled = _record_failure(company, error)
    company.last_fetched_at = now  # an attempt counts, so selection rotates past dead boards
    await _schedule_next(db, company, failed=True)  # and its next slot backs off exponentially
    if disabled:
        await db.execute(
            sa.update(Job).where(Job.source == company.ats, Job.company_id == company.id, Job.is_active.is_(True)).values(is_active=False)
        )
    await db.commit()


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
        from jobfinder.ingest.recipes import run_recipe

        return partial(run_recipe, recipe=company.recipe)
    return ATS_FETCHERS.get(company.ats)


def _recipe_run(company: Company, n_postings: int, error: str | None = None) -> bool:
    """Recipe boards only: fold the run into the recipe's health (committed with the run's other changes).
    True when the recipe is now degraded and due a re-induction (see _queue_reinduce)."""
    if company.ats != "recipe":
        return False
    from jobfinder.ingest.recipes.health import needs_reinduce, record_run

    record_run(company, n_postings, error)
    return needs_reinduce(company)


async def _queue_reinduce(db: AsyncSession, company_id: int) -> None:
    """Queue the LLM self-heal for a degraded recipe board (after the run is committed: enqueue commits)."""
    if not get_settings().resolver_llm_enabled:
        return
    from jobfinder.scheduling import queue

    try:
        await queue.enqueue(db, "reinduce_recipe", {"company_id": company_id}, dedupe=True)
    except Exception:  # the run itself is recorded; the next degraded run queues it again
        log.exception("could not queue re-induction for board %s", company_id)
        await db.rollback()


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
        active = await db.scalar(
            sa.select(sa.func.count()).select_from(Job).where(
                Job.source == company.ats, Job.company_id == company.id, Job.is_active.is_(True)
            )
        )
        streak = _empty_streak(company) + 1
        if active and active > EMPTY_GUARD_MIN_ACTIVE and streak < EMPTY_CONFIRM_RUNS:
            # suspicious: keep the jobs. Not a failure (consecutive_failures untouched), so it never
            # disables the board; the streak lives in last_error until it is confirmed or a fetch succeeds.
            stats.error = f"{EMPTY_ERROR_PREFIX}; kept {active} active jobs (run {streak}/{EMPTY_CONFIRM_RUNS})"
            company.last_error = stats.error
            company.last_fetched_at = datetime.now(UTC)
            heal = _recipe_run(company, 0)
            await _schedule_next(db, company)
            await db.commit()
            if heal:
                await _queue_reinduce(db, company.id)
            return stats
        stats.deactivated = await deactivate_missing(db, company.ats, company.id, [])
    now = datetime.now(UTC)
    _record_success(company, now)
    company.last_fetched_at = now
    heal = _recipe_run(company, len(postings))
    await _schedule_next(db, company)
    await db.commit()
    if heal:
        await _queue_reinduce(db, company.id)
    return stats


async def ingest_boards(
    session_factory: SessionFactory, *, ats: str | None = None, slug: str | None = None,
    concurrency: int | None = None, all_boards: bool = False,
) -> list[IngestStats]:
    """Poll every enabled company board, or with the adaptive schedule only those that are due
    (an explicit `ats`/`slug` or `all_boards` always fetches). One company failing never affects
    the others."""
    async with session_factory() as db:
        q = sa.select(Company).where(Company.enabled.is_(True)).order_by(Company.id)
        if ats:
            q = q.where(Company.ats == ats)
        if slug:
            q = q.where(Company.slug == slug)
        if get_settings().adaptive_schedule_enabled and not (ats or slug or all_boards):
            q = q.where(sa.or_(Company.next_fetch_at.is_(None), Company.next_fetch_at <= schedule.now()))
        boards = list((await db.scalars(q)).all())
        if not slug:  # an explicit board is always fetched; otherwise cap the discovered ones
            statuses = (await db.scalars(
                sa.select(Profile.status).join(User, User.id == Profile.user_id).where(User.activated_at.is_not(None))
            )).all()
            boards = select_boards(
                boards, [Target.from_status(s) for s in statuses], get_settings().ingest_max_discovered_boards_per_run
            )
        ids = [b.id for b in boards]

    sem = asyncio.Semaphore(concurrency or get_settings().ingest_concurrency)
    headers = {"User-Agent": "JobFinder/0.1 (+personal job search)"}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
        async def one(company_id: int) -> IngestStats:
            async with sem, session_factory() as db:
                # plain values, captured up front: after a rollback the ORM object is expired and
                # touching it in async code raises (MissingGreenlet / PendingRollbackError)
                name, board_ats = "?", "?"
                try:
                    company = await db.get(Company, company_id)
                    name, board_ats = company.name, company.ats
                    return await ingest_company(db, client, company)
                except Exception as e:  # last line of defence: never lose the whole run
                    log.exception("ingest failed for %s", name)
                    error = _describe(e)
                    try:  # still count it toward the board's health
                        await db.rollback()
                        company = await db.get(Company, company_id)
                        await _fail_board(db, company, error, datetime.now(UTC))
                    except Exception:
                        log.exception("could not record failure for %s", name)
                    return IngestStats(source=board_ats, company=name, error=error)

        results = await asyncio.gather(*(one(i) for i in ids), return_exceptions=True)
        out: list[IngestStats] = []
        for company_id, r in zip(ids, results, strict=True):
            if isinstance(r, BaseException):  # one() swallows Exception; this catches e.g. cancellation
                log.error("board task crashed", exc_info=r)
                r = IngestStats(source="?", company=f"board {company_id}", error=_describe(r) if isinstance(r, Exception) else type(r).__name__)
            out.append(r)
        return out
