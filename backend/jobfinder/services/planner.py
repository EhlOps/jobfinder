"""Background planner: decides which users get matching, audit and question work, and when.

One `plan_work` tick looks at every activated user with a questionnaire, ranks the ones that need
work, and queues tasks for the top few (staggered), so the shared Claude subscription is spent where
it helps most and never all at once. User actions still enqueue directly; this only adds work for
people who are offline."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from jobfinder.core.config import settings
from jobfinder.core.db import AsyncSession
from jobfinder.models import Profile, User
from jobfinder.repositories import planner as repo
from jobfinder.services.matching.service import (
    BUDGET_WINDOW,
    STALE_STATUSES,
    UNLIMITED,
    sync_career_stage,
)
from jobfinder.tasks import ledger

log = logging.getLogger("jobfinder.planner")
WORK_KINDS = ("match_user", "audit_profile")  # tasks that spend a user's AI budget
BACKOFF_ERRORS = ("rate_limited", "auth")
ACTIVE_WITHIN = timedelta(days=3)
IDLE_AFTER = timedelta(days=14)
STALE_AFTER = timedelta(hours=24)
RECENT_GAP = timedelta(hours=24)  # users seen within ACTIVE_WITHIN..IDLE_AFTER
IDLE_GAP = timedelta(days=7)
NEW_JOBS_CAP = 50
AUDIT_RETRY_AFTER = timedelta(hours=6)  # after a failed audit, match instead of retrying it every tick


@dataclass
class UserView:
    user: User
    profile: Profile
    left: int
    new_jobs: int
    profile_changed: bool
    questions_pending: bool
    last_run_at: datetime | None
    last_planned_at: datetime | None
    audit_failed_at: datetime | None
    seen_ago: timedelta
    reason: str = ""
    skip: str = ""
    next_due_at: datetime | None = None
    priority: tuple = field(default_factory=tuple)


def _parse(ts: str | None) -> datetime | None:
    try:
        d = datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None
    return d.replace(tzinfo=UTC) if d and d.tzinfo is None else d


def min_gap(seen_ago: timedelta) -> timedelta:
    """Idle users are looked at less often."""
    if seen_ago <= ACTIVE_WITHIN:
        return timedelta(hours=settings.planner_min_interval_hours)
    return RECENT_GAP if seen_ago <= IDLE_AFTER else IDLE_GAP


async def backoff_until(db: AsyncSession, now: datetime) -> datetime | None:
    """While Claude's latest call failed with a rate limit or auth error, everything waits."""
    last = await repo.latest_ai_call(db)
    if last is None or last.error_kind not in BACKOFF_ERRORS:
        return None
    until = last.created_at + timedelta(minutes=settings.planner_backoff_minutes)
    return until if until > now else None


async def in_flight_users(db: AsyncSession) -> set[int]:
    return await repo.users_with_tasks(db, WORK_KINDS, ledger.ACTIVE)


async def _views(db: AsyncSession, now: datetime) -> list[UserView]:
    rows = await repo.candidates(db)
    # A graduation date that has passed changes what the user should see: bump the version before looking
    # for stale matches so the re-score is planned like any other profile change.
    bumped = []
    for _, profile, _ in rows:
        current = bool(profile.audit) and profile.audit.get("version", 0) >= profile.version
        stage_changed = sync_career_stage(profile, now.date())  # every profile, not just the first change
        if stage_changed and current:
            # A stage-only bump (a date passing) is not worth a full AI audit; the re-score still runs.
            profile.audit = {**profile.audit, "version": profile.version}
        bumped.append(stage_changed)
    if any(bumped):
        await db.commit()
    used = await repo.scored_counts(db, now - BUDGET_WINDOW)
    changed = await repo.users_with_stale_matches(db, STALE_STATUSES)
    uncollected = await repo.users_with_uncollected(db)
    audit_failed = await repo.last_failed(db, "audit_profile", now - AUDIT_RETRY_AFTER)
    last_runs = {profile.user_id: _parse((profile.match_summary or {}).get("last_run_at")) for _, profile, _ in rows}
    total_jobs = await repo.active_jobs(db)
    # Jobs first seen at or before a user's last run are old news to them; the rest are new. Users with no
    # last run have no row here and see every active job as new.
    before_run = await repo.active_jobs_seen_by(db, [(uid, ts) for uid, ts in last_runs.items() if ts])
    defaults = settings.match_daily_llm_budget
    out = []
    for user, profile, state in rows:
        if user.match_budget_enabled is False:
            left = UNLIMITED
        else:
            left = max(0, (user.match_budget if user.match_budget is not None else defaults) - used.get(user.id, 0))
        last_run = last_runs[user.id]
        new_jobs = total_jobs - before_run.get(user.id, 0)
        out.append(UserView(
            user=user, profile=profile, left=left, new_jobs=new_jobs, profile_changed=user.id in changed,
            questions_pending=user.id in uncollected, last_run_at=last_run,
            last_planned_at=state.last_planned_at if state else None, audit_failed_at=audit_failed.get(user.id),
            seen_ago=now - user.last_seen_at if user.last_seen_at else IDLE_AFTER + timedelta(days=1),
        ))
    return out


def assess(v: UserView, now: datetime) -> bool:
    """Decide if the user needs work now; fills reason/skip/next_due_at/priority. True = eligible."""
    gap = min_gap(v.seen_ago)
    ref = max((t for t in (v.last_run_at, v.last_planned_at) if t), default=None)
    due = ref + gap if ref else now
    v.next_due_at = max(due, now)
    stale_for = now - v.last_run_at if v.last_run_at else None
    if v.profile_changed:
        v.reason = "profile changed"
    elif v.new_jobs > 0 and v.last_run_at:
        v.reason = f"{v.new_jobs} new jobs"
    elif stale_for is None:
        v.reason = "not run yet"
    elif stale_for >= STALE_AFTER:
        v.reason = "stale"
    if v.left <= 0:
        v.skip, v.reason = "budget exhausted", ""
        v.next_due_at = max(due, now + timedelta(minutes=settings.planner_interval_minutes))
        return False
    if due > now:
        v.skip, v.reason = "ran recently" if v.seen_ago <= ACTIVE_WITHIN else "inactive user, throttled", ""
        return False
    if not v.reason:
        v.skip = "nothing new"
        return False
    hours = (stale_for.total_seconds() / 3600) if stale_for else 24 * 7
    v.priority = (v.profile_changed, min(v.new_jobs, NEW_JOBS_CAP) + min(hours, 168) / 6, v.left)
    return True


def _needs_audit(v: UserView) -> bool:
    audited = (v.profile.audit or {}).get("version", 0)
    return bool(v.profile.background) and audited < v.profile.version and v.seen_ago <= IDLE_AFTER and v.audit_failed_at is None


async def _save_state(db: AsyncSession, views: list[UserView], now: datetime, planned: set[int]) -> None:
    if not views:
        return
    # last_planned_at keeps its stored value for users not planned this tick (COALESCE), so one statement covers everyone.
    rows = [
        {
            "user_id": v.user.id, "next_due_at": v.next_due_at, "last_reason": v.reason[:200], "last_skip": v.skip[:200],
            "last_planned_at": now if v.user.id in planned else None,
        }
        for v in views
    ]
    await repo.save_states(db, rows)
    await db.commit()


async def plan(db: AsyncSession, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    s = settings
    until = await backoff_until(db, now)
    if until:
        return {"skipped": "rate_limited", "until": until.isoformat()}
    busy = await in_flight_users(db)
    slots = s.planner_max_per_tick - len(busy)
    views = await _views(db, now)
    eligible = [v for v in views if v.user.id not in busy and assess(v, now)]
    for v in views:
        if v.user.id in busy:
            v.reason, v.skip, v.next_due_at = "", "work already queued", now + timedelta(minutes=s.planner_interval_minutes)
    eligible.sort(key=lambda v: v.priority, reverse=True)
    chosen = eligible[: max(0, slots)]
    for v in eligible[max(0, slots):]:
        v.skip, v.reason = "waiting for a free slot", ""
        v.next_due_at = now + timedelta(minutes=s.planner_interval_minutes)
    queued = {"match_user": 0, "audit_profile": 0, "collect_questions": 0}
    for i, v in enumerate(chosen):
        run_after = now + timedelta(seconds=i * s.planner_stagger_seconds)
        uid = v.user.id
        # An audit queues the match run itself, so it takes the user's slot instead of a separate match.
        kind = "audit_profile" if _needs_audit(v) else "match_user"
        await ledger.add(db, kind, user_id=uid, run_after=run_after, dedupe=True)
        queued[kind] += 1
        if v.questions_pending:
            await ledger.add(db, "collect_questions", user_id=uid, run_after=run_after, dedupe=True)
            queued["collect_questions"] += 1
        v.next_due_at = now + min_gap(v.seen_ago)
    await _save_state(db, views, now, {v.user.id for v in chosen})
    skipped: dict[str, int] = {}
    for v in views:
        if v.skip:
            skipped[v.skip] = skipped.get(v.skip, 0) + 1
    log.info("planned %d of %d users: %s", len(chosen), len(views), queued)
    return {"considered": len(views), "queued": queued, "skipped": skipped}
