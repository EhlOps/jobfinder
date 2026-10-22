"""Admin views' reads: the planner schedule per user, the AI's last call, and daily scoring counts."""
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import AICall, JobMatch, PlannerState, Profile, Task, User


async def last_ai_call(db: AsyncSession) -> AICall | None:
    return await db.scalar(sa.select(AICall).order_by(AICall.id.desc()).limit(1))


async def active_user_tasks(db: AsyncSession, statuses: tuple[str, ...]) -> list[Task]:
    """Unfinished tasks that belong to a user, soonest first."""
    return list((await db.scalars(
        sa.select(Task).where(Task.status.in_(statuses), Task.user_id.is_not(None)).order_by(Task.run_after)
    )).all())


async def schedule_users(db: AsyncSession) -> list[tuple[User, Profile | None, PlannerState | None]]:
    """Every user who can sign in, with their profile and planner state, by email."""
    rows = (await db.execute(
        sa.select(User, Profile, PlannerState).outerjoin(Profile, Profile.user_id == User.id)
        .outerjoin(PlannerState, PlannerState.user_id == User.id)
        .where(User.password_hash.is_not(None)).order_by(User.email)
    )).all()
    return [(u, p, st) for u, p, st in rows]


async def scored_since(db: AsyncSession, user_ids: list[int], since: datetime) -> dict[int, int]:
    """Matches scored since `since`, per user, in one query (users with none are absent)."""
    if not user_ids:
        return {}
    return dict((await db.execute(
        sa.select(JobMatch.user_id, sa.func.count())
        .where(JobMatch.user_id.in_(user_ids), JobMatch.scored_at >= since)
        .group_by(JobMatch.user_id)
    )).all())


async def last_task_created(db: AsyncSession, kind: str) -> datetime | None:
    return await db.scalar(sa.select(Task.created_at).where(Task.kind == kind).order_by(Task.id.desc()).limit(1))


async def lane_stats(db: AsyncSession, since: datetime, slow_wait_seconds: int) -> list[dict]:
    """Per queue (by name), over tasks claimed since `since`: queue, tasks, wait_p50/p95, run_p50/p95 and
    slow_waits (waits over `slow_wait_seconds`), in seconds. The wait is started_at - run_after (the
    latest attempt's); the run is finished_at - started_at, over the finished ones only (None while none
    has finished). A row whose run_after moved past its started_at (a retry in backoff, an admin requeue,
    or one of those expired unclaimed) has no claim of its current attempt: it is left out, so no wait is
    negative and no run spans a backoff."""
    wait = sa.extract("epoch", Task.started_at - Task.run_after)
    run = sa.extract("epoch", Task.finished_at - Task.started_at)

    def pct(p: float, of, name: str):
        return sa.func.percentile_cont(p).within_group(of).label(name)

    return [dict(r) for r in (await db.execute(
        sa.select(Task.queue, sa.func.count().label("tasks"), pct(0.5, wait, "wait_p50"), pct(0.95, wait, "wait_p95"),
                  pct(0.5, run, "run_p50"), pct(0.95, run, "run_p95"),
                  sa.func.count().filter(wait > slow_wait_seconds).label("slow_waits"))
        .where(Task.started_at >= since, Task.started_at >= Task.run_after)
        .group_by(Task.queue).order_by(Task.queue)
    )).mappings().all()]
