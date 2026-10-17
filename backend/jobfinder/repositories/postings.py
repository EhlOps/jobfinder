"""Job writes for ingestion: upsert fetched postings, deactivate the ones a board dropped, expire stale ones.

Plus the few job reads ingestion decides on (a board's active count and churn). Nothing here commits.
"""
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.ingest.base import JobPosting, safe_http_url
from jobfinder.models import Job

UPSERT_CHUNK = 200
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


async def upsert(db: AsyncSession, postings: list[JobPosting], company_id: int | None) -> tuple[int, int]:
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
    return new, updated


async def deactivate_missing(db: AsyncSession, source: str, company_id: int, seen_ids: list[str]) -> int:
    """Deactivate the board's active jobs whose external id is not in `seen_ids`."""
    res = await db.execute(
        sa.update(Job)
        .where(
            Job.source == source, Job.company_id == company_id, Job.is_active.is_(True),
            sa.not_(Job.external_id == sa.any_(sa.bindparam("ids", seen_ids, type_=ARRAY(sa.String)))),
        )
        .values(is_active=False)
    )
    return res.rowcount or 0


async def deactivate_board(db: AsyncSession, source: str, company_id: int) -> None:
    """Deactivate every active job of a board (it was just disabled)."""
    await db.execute(
        sa.update(Job).where(Job.source == source, Job.company_id == company_id, Job.is_active.is_(True))
        .values(is_active=False)
    )


async def expire_stale(db: AsyncSession, source: str, older_than: timedelta) -> int:
    """For sources without per-company boards (jobspy, usajobs): expire jobs not seen recently."""
    res = await db.execute(
        sa.update(Job)
        .where(Job.source == source, Job.is_active.is_(True), Job.last_seen_at < datetime.now(UTC) - older_than)
        .values(is_active=False)
    )
    return res.rowcount or 0


async def count_active(db: AsyncSession, source: str, company_id: int) -> int:
    return await db.scalar(
        sa.select(sa.func.count()).select_from(Job).where(
            Job.source == source, Job.company_id == company_id, Job.is_active.is_(True)
        )
    ) or 0


async def first_seen(db: AsyncSession, company_id: int) -> datetime | None:
    """When the board's first job was seen (None: it has none)."""
    return await db.scalar(sa.select(sa.func.min(Job.first_seen_at)).where(Job.company_id == company_id))


async def count_new_since(db: AsyncSession, company_id: int, since: datetime) -> int:
    return await db.scalar(
        sa.select(sa.func.count()).select_from(Job).where(Job.company_id == company_id, Job.first_seen_at > since)
    ) or 0


async def id_by_external(db: AsyncSession, source: str, external_id: str) -> int | None:
    return await db.scalar(sa.select(Job.id).where(Job.source == source, Job.external_id == external_id))
