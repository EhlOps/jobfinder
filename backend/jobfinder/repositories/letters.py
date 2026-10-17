"""Cover letter rows: one current draft per (user, job)."""
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import CoverLetter


async def get(db: AsyncSession, user_id: int, job_id: int) -> CoverLetter | None:
    return await db.scalar(sa.select(CoverLetter).where(CoverLetter.user_id == user_id, CoverLetter.job_id == job_id))


async def job_ids_with_letters(db: AsyncSession, user_id: int, job_ids: list[int]) -> set[int]:
    """Which of these jobs the user has a letter for, in one query."""
    if not job_ids:
        return set()
    rows = await db.scalars(
        sa.select(CoverLetter.job_id).where(CoverLetter.user_id == user_id, CoverLetter.job_id.in_(job_ids))
    )
    return set(rows)


async def save(db: AsyncSession, letter: CoverLetter) -> CoverLetter:
    """Write the letter's pending changes and reload it, so server-set columns (updated_at) are current."""
    await db.flush()
    await db.refresh(letter)
    return letter


async def delete(db: AsyncSession, user_id: int, job_id: int) -> None:
    await db.execute(sa.delete(CoverLetter).where(CoverLetter.user_id == user_id, CoverLetter.job_id == job_id))


async def upsert_draft(
    db: AsyncSession, user_id: int, job_id: int, *, content: str, tone: str, profile_version: int,
) -> None:
    """Store a freshly generated draft, replacing any earlier one (and clearing its edited flag)."""
    now = datetime.now(UTC)
    values = {
        "user_id": user_id, "job_id": job_id, "content": content, "tone": tone, "edited": False,
        "profile_version": profile_version, "generated_at": now, "updated_at": now,
    }
    stmt = insert(CoverLetter).values(**values)
    await db.execute(
        stmt.on_conflict_do_update(
            constraint="uq_letter_user_job", set_={k: stmt.excluded[k] for k in values if k not in ("user_id", "job_id")}
        )
    )
