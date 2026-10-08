"""Connect Claude from the web app: the admin pastes the token from `claude setup-token`."""
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.ai import credentials
from jobfinder.auth.security import current_user, require_admin
from jobfinder.config import get_settings
from jobfinder.db import get_db
from jobfinder.matching.service import UNLIMITED, remaining_budget
from jobfinder.models import AICall, Company, Job, PlannerState, Profile, Task, User
from jobfinder.scheduling import planner, queue

router = APIRouter(tags=["ai"])

DB = Annotated[AsyncSession, Depends(get_db)]
Admin = Annotated[User, Depends(require_admin)]


class LastCall(BaseModel):
    ok: bool
    error_kind: str | None
    at: str


class AiStatus(BaseModel):
    configured: bool
    source: Literal["file", "env", "none"]
    last_call: LastCall | None


class TokenIn(BaseModel):
    token: str


class TaskRef(BaseModel):
    task_id: int


@router.get("/api/ai/status", response_model=AiStatus)
async def ai_status(_: Annotated[User, Depends(current_user)], db: DB):
    """Any signed-in user may see whether the AI is connected (never the token itself)."""
    src = credentials.source()
    row = (await db.execute(select(AICall).order_by(AICall.id.desc()).limit(1))).scalar_one_or_none()
    last = LastCall(ok=row.ok, error_kind=row.error_kind, at=row.created_at.isoformat()) if row else None
    return AiStatus(configured=src != "none", source=src, last_call=last)


@router.put("/api/admin/ai/token", response_model=TaskRef, status_code=202)
async def set_token(body: TokenIn, admin: Admin, db: DB):
    try:
        credentials.write_token(body.token)
    except credentials.InvalidToken as e:
        raise HTTPException(422, str(e)) from None
    task = await queue.enqueue(db, "ai_check", user_id=admin.id)  # verify it right away
    return TaskRef(task_id=task.id)


@router.post("/api/admin/ai/check", response_model=TaskRef, status_code=202)
async def check(admin: Admin, db: DB):
    task = await queue.enqueue(db, "ai_check", user_id=admin.id, dedupe=True)
    return TaskRef(task_id=task.id)


@router.delete("/api/admin/ai/token", status_code=204)
async def delete_token(_: Admin):
    credentials.delete_token()


class ActiveTask(BaseModel):
    kind: str
    status: str
    run_after: str


class ScheduleRow(BaseModel):
    user_id: int
    email: str
    last_seen_at: str | None
    last_run_at: str | None
    last_scored: int | None
    pending: int | None
    budget_left: int | None  # null = no daily limit
    last_planned_at: str | None
    next_due_at: str | None
    reason: str
    skip: str
    active: list[ActiveTask]


class Schedule(BaseModel):
    backoff_until: str | None
    in_flight: int
    max_per_tick: int
    next_tick: str | None
    users: list[ScheduleRow]


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


@router.get("/api/admin/schedule", response_model=Schedule)
async def schedule(_: Admin, db: DB):
    """What the background planner has done and will do for each user."""
    now = datetime.now(UTC)
    active_tasks = (
        await db.scalars(select(Task).where(Task.status.in_(queue.ACTIVE), Task.user_id.is_not(None)).order_by(Task.run_after))
    ).all()
    by_user: dict[int, list[ActiveTask]] = {}
    for t in active_tasks:
        by_user.setdefault(t.user_id, []).append(ActiveTask(kind=t.kind, status=t.status, run_after=t.run_after.isoformat()))
    rows = (
        await db.execute(
            select(User, Profile, PlannerState).outerjoin(Profile, Profile.user_id == User.id)
            .outerjoin(PlannerState, PlannerState.user_id == User.id)
            .where(User.password_hash.is_not(None)).order_by(User.email)
        )
    ).all()
    users = []
    for u, p, st in rows:
        left = await remaining_budget(db, u.id)
        ms = (p.match_summary if p else None) or {}
        users.append(ScheduleRow(
            user_id=u.id, email=u.email, last_seen_at=_iso(u.last_seen_at), last_run_at=ms.get("last_run_at"),
            last_scored=ms.get("scored"), pending=ms.get("pending"), budget_left=None if left >= UNLIMITED else left,
            last_planned_at=_iso(st.last_planned_at if st else None), next_due_at=_iso(st.next_due_at if st else None),
            reason=st.last_reason if st else "", skip=st.last_skip if st else "", active=by_user.get(u.id, []),
        ))
    last_tick = await db.scalar(select(Task.created_at).where(Task.kind == "plan_work").order_by(Task.id.desc()).limit(1))
    next_tick = last_tick + timedelta(minutes=get_settings().planner_interval_minutes) if last_tick else None
    return Schedule(
        backoff_until=_iso(await planner.backoff_until(db, now)), in_flight=len(await planner.in_flight_users(db)),
        max_per_tick=get_settings().planner_max_per_tick, next_tick=_iso(next_tick), users=users,
    )


class SourceRow(BaseModel):
    id: int
    name: str
    ats: str
    slug: str
    origin: str
    enabled: bool
    consecutive_failures: int
    last_success_at: str | None
    last_fetched_at: str | None
    last_error: str | None
    disabled_reason: str | None
    validated_at: str | None
    job_count: int


def _source_row(c: Company, job_count: int) -> SourceRow:
    return SourceRow(
        id=c.id, name=c.name, ats=c.ats, slug=c.slug, origin=c.origin, enabled=c.enabled,
        consecutive_failures=c.consecutive_failures, last_success_at=_iso(c.last_success_at),
        last_fetched_at=_iso(c.last_fetched_at), last_error=c.last_error, disabled_reason=c.disabled_reason,
        validated_at=_iso(c.validated_at), job_count=job_count,
    )


@router.get("/api/admin/sources", response_model=list[SourceRow])
async def sources(_: Admin, db: DB):
    """Per-board ingest health, failing boards first."""
    counts = dict(
        (await db.execute(select(Job.company_id, func.count()).where(Job.is_active.is_(True)).group_by(Job.company_id))).all()
    )
    rows = (await db.scalars(select(Company).order_by(Company.enabled, Company.consecutive_failures.desc(), Company.name, Company.id))).all()
    return [_source_row(c, counts.get(c.id, 0)) for c in rows]


@router.post("/api/admin/sources/{company_id}/enable", response_model=SourceRow)
async def enable_source(company_id: int, _: Admin, db: DB):
    """Re-enable a disabled board; counters reset so one more failure does not disable it again."""
    c = await db.get(Company, company_id)
    if c is None:
        raise HTTPException(404, "No such source")
    if not c.enabled:  # a board that is merely failing keeps its count and error
        c.enabled = True
        c.consecutive_failures = 0
        c.disabled_reason = None
        c.last_error = None
        await db.commit()
    n = await db.scalar(select(func.count()).select_from(Job).where(Job.company_id == c.id, Job.is_active.is_(True)))
    return _source_row(c, n or 0)
