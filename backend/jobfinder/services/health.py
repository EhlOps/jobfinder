"""What the readiness probe asks the database (api/health.py decides what the answer means)."""
from jobfinder.core.db import AsyncSession
from jobfinder.repositories import migrations


async def schema_revisions(db: AsyncSession) -> frozenset[str] | None:
    """The database's alembic revisions; None when it has no alembic_version table."""
    return await migrations.alembic_revisions(db)
