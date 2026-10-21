"""The planner's reads and its state (services/planner.py): who is due work, what each user has used and
missed, and the planner_state row it keeps per user. Batched: one query per fact for every user."""
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import AICall, Job, JobMatch, PlannerState, Profile, Task, User

STATE_CHUNK = 1000  # planner_state rows per statement: keeps the bind-parameter count under the driver limit


async def latest_ai_call(db: AsyncSession) -> AICall | None:
    return (await db.execute(sa.select(AICall).order_by(AICall.id.desc()).limit(1))).scalar_one_or_none()


async def users_with_tasks(db: AsyncSession, kinds: Sequence[str], statuses: Sequence[str]) -> set[int]:
    rows = await db.scalars(
        sa.select(Task.user_id).where(Task.kind.in_(kinds), Task.status.in_(statuses), Task.user_id.is_not(None))
    )
    return set(rows)


async def candidates(db: AsyncSession) -> list[tuple[User, Profile, PlannerState | None]]:
    """Activated users with a filled-in status, their profile and planner state, by id."""
    rows = await db.execute(
        sa.select(User, Profile, PlannerState)
        .join(Profile, Profile.user_id == User.id)
        .outerjoin(PlannerState, PlannerState.user_id == User.id)
        .where(User.password_hash.is_not(None), Profile.status != {})
        .order_by(User.id)
    )
    return [(u, p, s) for u, p, s in rows.all()]


async def scored_counts(db: AsyncSession, since: datetime) -> dict[int, int]:
    """Matches scored per user since `since`."""
    return dict((await db.execute(
        sa.select(JobMatch.user_id, sa.func.count()).where(JobMatch.scored_at >= since).group_by(JobMatch.user_id)
    )).all())


async def users_with_stale_matches(db: AsyncSession, statuses: Sequence[str]) -> set[int]:
    """Users with a match at one of `statuses`, on an active job, scored against an older profile version."""
    return set(await db.scalars(
        sa.select(JobMatch.user_id).join(Job, Job.id == JobMatch.job_id).join(Profile, Profile.user_id == JobMatch.user_id)
        .where(JobMatch.status.in_(statuses), JobMatch.profile_version < Profile.version, Job.is_active.is_(True))
        .distinct()
    ))


async def users_with_uncollected(db: AsyncSession) -> set[int]:
    """Users with a match on an active job whose questions are not collected yet."""
    return set(await db.scalars(
        sa.select(JobMatch.user_id).join(Job, Job.id == JobMatch.job_id)
        .where(JobMatch.questions_collected.is_(False), Job.is_active.is_(True)).distinct()
    ))


async def last_failed(db: AsyncSession, kind: str, since: datetime) -> dict[int, datetime]:
    """Per user, when their latest task of `kind` that failed at or after `since` finished."""
    return dict((await db.execute(
        sa.select(Task.user_id, sa.func.max(Task.finished_at))
        .where(Task.kind == kind, Task.status == "failed", Task.finished_at >= since, Task.user_id.is_not(None))
        .group_by(Task.user_id)
    )).all())


async def active_jobs(db: AsyncSession) -> int:
    return await db.scalar(sa.select(sa.func.count()).select_from(Job).where(Job.is_active.is_(True))) or 0


async def active_jobs_seen_by(db: AsyncSession, runs: list[tuple[int, datetime]]) -> dict[int, int]:
    """For each (user_id, last_run): active jobs first seen at or before that run."""
    if not runs:
        return {}
    seen_runs = sa.values(sa.column("user_id", sa.Integer), sa.column("last_run", sa.DateTime(timezone=True)), name="runs").data(runs)
    return dict((await db.execute(
        sa.select(seen_runs.c.user_id, sa.func.count(Job.id))
        .select_from(seen_runs.join(Job, sa.and_(Job.first_seen_at <= seen_runs.c.last_run, Job.is_active.is_(True))))
        .group_by(seen_runs.c.user_id)
    )).all())


async def save_states(db: AsyncSession, rows: list[dict]) -> None:
    """Upsert planner_state rows. A None last_planned_at keeps the stored value (COALESCE)."""
    for i in range(0, len(rows), STATE_CHUNK):
        stmt = insert(PlannerState).values(rows[i : i + STATE_CHUNK])
        await db.execute(stmt.on_conflict_do_update(
            index_elements=["user_id"],
            set_={
                "next_due_at": stmt.excluded.next_due_at, "last_reason": stmt.excluded.last_reason, "last_skip": stmt.excluded.last_skip,
                "last_planned_at": sa.func.coalesce(stmt.excluded.last_planned_at, PlannerState.last_planned_at),
            },
        ))
