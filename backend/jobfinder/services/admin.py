"""Admin rules: board health and re-enabling, coverage pins, the planner schedule, and the Claude token.

`enable_source` is the one re-enable rule, shared by Admin › Sources and `cli set-board --enabled yes`.
"""
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.core.config import settings
from jobfinder.core.errors import Invalid, NotFound
from jobfinder.ingest import coveted
from jobfinder.integrations.claude import credentials
from jobfinder.matching.service import BUDGET_WINDOW, UNLIMITED
from jobfinder.models import Company, CovetedTarget, User
from jobfinder.repositories import admin as repo
from jobfinder.repositories import companies, coverage
from jobfinder.scheduling import planner
from jobfinder.schemas.ai_admin import (
    ActiveTask,
    AiStatus,
    IngestCadence,
    LastCall,
    Schedule,
    ScheduleRow,
    SourceRow,
    TierCount,
)
from jobfinder.schemas.coverage import Coverage, CoverageRow, PinIn, PinOut, TargetRow, UnpinIn
from jobfinder.tasks import ledger

BAD_DOMAIN = "Enter the employer's bare domain, like acme.com (no https://, path or port)."


class NoSuchSource(NotFound):
    code, message = "no_such_source", "No such source"


class NoSuchCompany(NotFound):
    code, message = "no_such_company", "No such company"


class NoSuchEmployer(NotFound):
    code, message = "no_such_employer", "No such employer"


class NeedName(Invalid):
    code, message = "need_name", "Give the employer's name."


class BadDomain(Invalid):
    code, message = "bad_domain", BAD_DOMAIN


class NeedTarget(Invalid):
    code, message = "need_target", "Say which company or employer to unpin."


class BadToken(Invalid):
    code = "bad_token"


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


# --- sources -------------------------------------------------------------------------------------

def _source_row(c: Company, job_count: int) -> SourceRow:
    return SourceRow(
        id=c.id, name=c.name, ats=c.ats, slug=c.slug, origin=c.origin, enabled=c.enabled,
        consecutive_failures=c.consecutive_failures, last_success_at=_iso(c.last_success_at),
        last_fetched_at=_iso(c.last_fetched_at), last_error=c.last_error, disabled_reason=c.disabled_reason,
        validated_at=_iso(c.validated_at), job_count=job_count,
    )


async def sources(db: AsyncSession) -> list[SourceRow]:
    """Per-board ingest health, failing boards first."""
    counts = await companies.active_job_counts(db)
    return [_source_row(c, counts.get(c.id, 0)) for c in await companies.for_sources(db)]


async def enable_source(db: AsyncSession, company_id: int) -> Company:
    """Re-enable a disabled board; counters reset so one more failure does not disable it again."""
    c = await companies.get(db, company_id)
    if c is None:
        raise NoSuchSource()
    if not c.enabled:  # a board that is merely failing keeps its count and error
        c.enabled = True
        c.consecutive_failures = 0
        c.disabled_reason = None
        c.last_error = None
        c.next_fetch_at = None  # due on the next tick, not after the failure backoff
    return c


async def enable(db: AsyncSession, company_id: int) -> SourceRow:
    c = await enable_source(db, company_id)
    counts = await companies.active_job_counts(db, {c.id})
    return _source_row(c, counts.get(c.id, 0))


# --- coverage ------------------------------------------------------------------------------------

def _target_row(t: CovetedTarget) -> TargetRow:
    return TargetRow(
        id=t.id, name=t.name, domain=t.domain, source=t.source, score=t.score, last_resolved_at=_iso(t.last_resolved_at),
        last_error=t.last_error, careers_url=coveted.candidate_urls(t.domain)[0],
    )


async def coverage_view(db: AsyncSession) -> Coverage:
    """Coveted employers (tier 1-2, or pinned/unpinned by an admin), most coveted first, plus unresolved targets."""
    now = datetime.now(UTC)
    rows_c = await companies.coveted(db)
    counts = await companies.active_job_counts(db, {c.id for c in rows_c})
    targets = await coverage.all_targets(db)
    by_company: dict[int, CovetedTarget] = {}
    for t in targets:
        if t.company_id is not None:
            by_company.setdefault(t.company_id, t)
    rows = []
    for c in rows_c:
        t = by_company.get(c.id)
        rows.append(CoverageRow(
            company_id=c.id, name=c.name, ats=c.ats, slug=c.slug, tier=c.coveted_tier or 3, score=c.coveted_score,
            pinned=c.coveted_pinned, board_status=coveted.board_status(c, now), gap=coveted.is_gap(c, now),
            job_count=counts.get(c.id, 0), last_success_at=_iso(c.last_success_at), recipe_state=c.recipe_state,
            last_error=c.last_error or c.disabled_reason, resolve_error=t.last_error if t else None,
            last_resolved_at=_iso(t.last_resolved_at) if t else None, domain=t.domain if t else None,
            careers_url=coveted.candidate_urls(t.domain)[0] if t else None,
        ))
    rows.sort(key=lambda r: (r.tier, -r.score, r.name.lower(), r.company_id))
    unresolved = sorted((t for t in targets if t.company_id is None), key=lambda t: (-t.score, t.name.lower(), t.id))
    return Coverage(companies=rows, targets=[_target_row(t) for t in unresolved])


def _pin_company(c: Company) -> None:
    c.coveted_pinned = True
    coveted.apply_score(c, c.coveted_score)  # pinned is tier 1 whatever its score


async def pin(db: AsyncSession, body: PinIn) -> PinOut:
    """Pin a known company, or add an employer we have no board for by name and domain; the latter is
    sent through the resolver right away."""
    if body.company_id is not None:
        c = await companies.get(db, body.company_id)
        if c is None:
            raise NoSuchCompany()
        _pin_company(c)
        return PinOut(company_id=c.id)
    name = (body.name or "").strip()
    if not name:
        raise NeedName()
    domain = coveted.clean_domain(body.domain or "")
    if domain is None:
        raise BadDomain()
    t = await coverage.by_domain(db, domain)
    if t is None:
        t = await coverage.add(db, CovetedTarget(name=name[:200], domain=domain, source="admin", score=100.0))
    else:  # an employer the audit already tracks: an admin now wants it too
        t.source, t.score = "admin", max(t.score, 100.0)
        if t.company_id is not None and (c := await companies.get(db, t.company_id)) is not None:
            _pin_company(c)
    task = None
    if t.company_id is None:
        task = await ledger.add(db, "audit_coverage", {"target_id": t.id}, dedupe=True)
    return PinOut(company_id=t.company_id, target_id=t.id, task_id=task.id if task else None)


async def unpin(db: AsyncSession, body: UnpinIn) -> PinOut:
    """Undo a pin. For a company, `exclude` marks it not coveted at all; for a target with no board, the
    target is dropped."""
    if body.target_id is not None:
        t = await coverage.get(db, body.target_id)
        if t is None:
            raise NoSuchEmployer()
        await coverage.delete(db, t)
        return PinOut(target_id=body.target_id)
    if body.company_id is None:
        raise NeedTarget()
    c = await companies.get(db, body.company_id)
    if c is None:
        raise NoSuchCompany()
    c.coveted_pinned = False if body.exclude else None
    coveted.apply_score(c, c.coveted_score)
    # an unpinned score is stale (an exclusion zeroes it): recompute without running a whole audit
    task = await ledger.add(db, "score_coveted", {"no_audit": True}, dedupe=True)
    return PinOut(company_id=c.id, task_id=task.id)


async def rescore(db: AsyncSession) -> int:
    return (await ledger.add(db, "score_coveted", dedupe=True)).id


async def resolve_target(db: AsyncSession, target_id: int) -> int:
    if await coverage.get(db, target_id) is None:
        raise NoSuchEmployer()
    return (await ledger.add(db, "audit_coverage", {"target_id": target_id}, dedupe=True)).id


async def resolve_company(db: AsyncSession, company_id: int) -> int:
    if await companies.get(db, company_id) is None:
        raise NoSuchCompany()
    return (await ledger.add(db, "audit_coverage", {"company_id": company_id}, dedupe=True)).id


# --- the AI token --------------------------------------------------------------------------------

async def ai_status(db: AsyncSession) -> AiStatus:
    """Whether the AI is connected (never the token itself) and how its last call went."""
    src = credentials.source()
    row = await repo.last_ai_call(db)
    last = LastCall(ok=row.ok, error_kind=row.error_kind, at=row.created_at.isoformat()) if row else None
    return AiStatus(configured=src != "none", source=src, last_call=last)


async def set_token(db: AsyncSession, admin: User, token: str) -> int:
    """Save the token from `claude setup-token` and queue a check of it; returns the check task's id."""
    try:
        credentials.write_token(token)
    except credentials.InvalidToken as e:
        raise BadToken(str(e)) from None
    return await check_ai(db, admin, dedupe=False)


async def check_ai(db: AsyncSession, admin: User, *, dedupe: bool = True) -> int:
    return (await ledger.add(db, "ai_check", user_id=admin.id, dedupe=dedupe)).id


def delete_token() -> None:
    credentials.delete_token()


# --- the planner schedule ------------------------------------------------------------------------

async def remaining_budgets(db: AsyncSession, users: list[User]) -> dict[int, int]:
    """Each user's scoring budget left today (UNLIMITED when they turned the limit off), in one query.
    The batched form of matching.service.remaining_budget."""
    used = await repo.scored_since(db, [u.id for u in users], datetime.now(UTC) - BUDGET_WINDOW)
    left = {}
    for u in users:
        if u.match_budget_enabled is False:
            left[u.id] = UNLIMITED
            continue
        limit = u.match_budget if u.match_budget is not None else settings.match_daily_llm_budget
        left[u.id] = max(0, limit - used.get(u.id, 0))
    return left


async def _ingest_cadence(db: AsyncSession, now: datetime) -> IngestCadence:
    s = settings
    return IngestCadence(
        adaptive=s.adaptive_schedule_enabled, tick_minutes=s.ingest_tick_minutes, interval_hours=s.ingest_interval_hours,
        tiers=[TierCount(tier=t, boards=n, due=d) for t, n, d in await companies.cadence_by_tier(db, now)],
    )


async def schedule(db: AsyncSession) -> Schedule:
    """What the background planner has done and will do for each user."""
    now = datetime.now(UTC)
    by_user: dict[int, list[ActiveTask]] = {}
    for t in await repo.active_user_tasks(db, ledger.ACTIVE):
        by_user.setdefault(t.user_id, []).append(ActiveTask(kind=t.kind, status=t.status, run_after=t.run_after.isoformat()))
    rows = await repo.schedule_users(db)
    budgets = await remaining_budgets(db, [u for u, _, _ in rows])
    users = []
    for u, p, st in rows:
        left = budgets[u.id]
        ms = (p.match_summary if p else None) or {}
        users.append(ScheduleRow(
            user_id=u.id, email=u.email, last_seen_at=_iso(u.last_seen_at), last_run_at=ms.get("last_run_at"),
            last_scored=ms.get("scored"), pending=ms.get("pending"), budget_left=None if left >= UNLIMITED else left,
            last_planned_at=_iso(st.last_planned_at if st else None), next_due_at=_iso(st.next_due_at if st else None),
            reason=st.last_reason if st else "", skip=st.last_skip if st else "", active=by_user.get(u.id, []),
        ))
    last_tick = await repo.last_task_created(db, "plan_work")
    next_tick = last_tick + timedelta(minutes=settings.planner_interval_minutes) if last_tick else None
    return Schedule(
        backoff_until=_iso(await planner.backoff_until(db, now)), in_flight=len(await planner.in_flight_users(db)),
        max_per_tick=settings.planner_max_per_tick, next_tick=_iso(next_tick), users=users,
        ingest=await _ingest_cadence(db, now),
    )
