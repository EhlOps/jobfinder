"""Ingestion: fetch boards, upsert into the `jobs` cache, expire jobs that disappeared."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import sqlalchemy as sa
import yaml
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.config import get_settings
from jobfinder.ingest.base import JobPosting, safe_http_url
from jobfinder.ingest.sources import ATS_FETCHERS
from jobfinder.models import Company, Job

log = logging.getLogger("jobfinder.ingest")

COMPANIES_YAML = Path(__file__).parent / "companies.yaml"
VALID_SIZES = {"startup", "mid", "large"}
UPSERT_CHUNK = 200
# An empty response from a board that had more than this many active jobs is treated as an
# API glitch: keep the jobs rather than deactivating the whole board.
EMPTY_GUARD_MIN_ACTIVE = 5
JOBSPY_EXPIRE_AFTER = timedelta(days=14)

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


# ── companies ─────────────────────────────────────────────────────────────
def load_seed() -> list[dict]:
    return yaml.safe_load(COMPANIES_YAML.read_text())["companies"]


async def sync_companies(db: AsyncSession, entries: list[dict] | None = None) -> int:
    """Upsert the seed list by (ats, slug). Returns the number of rows touched."""
    entries = load_seed() if entries is None else entries
    for e in entries:
        stmt = insert(Company).values(
            name=e["name"], ats=e["ats"], slug=e["slug"], prestige_tier=e.get("prestige", 3),
            size=e.get("size", "mid"), industry=e.get("industry", ""),
            sponsors_visas=e.get("sponsors_visas"),
        )
        await db.execute(
            stmt.on_conflict_do_update(
                constraint="uq_company_ats_slug",
                set_={"name": stmt.excluded.name, "prestige_tier": stmt.excluded.prestige_tier,
                      "size": stmt.excluded.size, "industry": stmt.excluded.industry,
                      "sponsors_visas": stmt.excluded.sponsors_visas},
            )
        )
    await db.commit()
    return len(entries)


# ── upsert / expire ───────────────────────────────────────────────────────
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
    """For sources without per-company boards (jobspy): expire jobs not seen recently."""
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


async def ingest_company(db: AsyncSession, client: httpx.AsyncClient, company: Company) -> IngestStats:
    stats = IngestStats(source=company.ats, company=company.name)
    fetcher = ATS_FETCHERS[company.ats]
    try:
        postings = await _fetch_with_retry(fetcher, client, company.slug, company.name)
    except Exception as e:
        stats.error = _describe(e)
        company.last_error = stats.error
        await db.commit()
        return stats

    stats.fetched = len(postings)
    if postings:
        stats.new, stats.updated = await upsert_postings(db, postings, company.id)
        stats.deactivated = await deactivate_missing(db, company.ats, company.id, [p.external_id for p in postings])
        company.last_error = None
    else:
        active = await db.scalar(
            sa.select(sa.func.count()).select_from(Job).where(
                Job.source == company.ats, Job.company_id == company.id, Job.is_active.is_(True)
            )
        )
        if active and active > EMPTY_GUARD_MIN_ACTIVE:
            stats.error = f"empty response; kept {active} active jobs"
            company.last_error = stats.error
        else:
            stats.deactivated = await deactivate_missing(db, company.ats, company.id, [])
            company.last_error = None
    company.last_fetched_at = datetime.now(UTC)
    await db.commit()
    return stats


async def ingest_boards(
    session_factory: SessionFactory, *, ats: str | None = None, slug: str | None = None,
    concurrency: int | None = None,
) -> list[IngestStats]:
    """Poll every enabled company board. One company failing never affects the others."""
    async with session_factory() as db:
        await sync_companies(db)
        q = sa.select(Company.id).where(Company.enabled.is_(True)).order_by(Company.id)
        if ats:
            q = q.where(Company.ats == ats)
        if slug:
            q = q.where(Company.slug == slug)
        ids = list((await db.scalars(q)).all())

    sem = asyncio.Semaphore(concurrency or get_settings().ingest_concurrency)
    headers = {"User-Agent": "JobFinder/0.1 (+personal job search)"}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
        async def one(company_id: int) -> IngestStats:
            async with sem, session_factory() as db:
                company = await db.get(Company, company_id)
                try:
                    return await ingest_company(db, client, company)
                except Exception as e:  # last line of defence: never lose the whole run
                    log.exception("ingest failed for %s", company.name)
                    return IngestStats(source=company.ats, company=company.name, error=_describe(e))

        return list(await asyncio.gather(*(one(i) for i in ids)))
