"""Task-ledger reads the API needs: the newest unfinished task of a kind for a match."""
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import Task


async def pending_for(db: AsyncSession, user_id: int, kind: str, match_id: int) -> int | None:
    """The id of the newest queued or running `kind` task for this match, if any."""
    return (
        await db.execute(
            sa.select(Task.id)
            .where(
                Task.kind == kind, Task.user_id == user_id, Task.status.in_(("queued", "running")),
                Task.payload["match_id"].as_integer() == match_id,
            )
            .order_by(Task.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
