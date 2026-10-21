"""The database's alembic revisions, for the readiness probe (api/health.py)."""
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession


async def alembic_revisions(db: AsyncSession) -> frozenset[str] | None:
    """The revisions in alembic_version; None when the table doesn't exist."""
    # to_regclass instead of selecting straight away: a missing table is an answer, not an error.
    if await db.scalar(sa.text("select to_regclass('alembic_version')")) is None:
        return None
    return frozenset((await db.scalars(sa.text("select version_num from alembic_version"))).all())
