"""Admin › Coverage: how sought-after each employer is, and the coveted ones we can't read yet."""
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, or_, select

from jobfinder.api.deps import AdminUser, DbSession
from jobfinder.ingest import coveted
from jobfinder.models import Company, CovetedTarget, Job
from jobfinder.scheduling import queue
from jobfinder.schemas.common import TaskRef
from jobfinder.schemas.coverage import Coverage, CoverageRow, PinIn, PinOut, TargetRow, UnpinIn

router = APIRouter(prefix="/api/admin/coverage", tags=["coverage"])

BAD_DOMAIN = "Enter the employer's bare domain, like acme.com (no https://, path or port)."


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _target_row(t: CovetedTarget) -> TargetRow:
    return TargetRow(
        id=t.id, name=t.name, domain=t.domain, source=t.source, score=t.score, last_resolved_at=_iso(t.last_resolved_at),
        last_error=t.last_error, careers_url=coveted.candidate_urls(t.domain)[0],
    )


@router.get("", response_model=Coverage)
async def coverage(_: AdminUser, db: DbSession):
    """Coveted employers (tier 1-2, or pinned/unpinned by an admin), most coveted first, plus unresolved targets."""
    now = datetime.now(UTC)
    companies = (await db.scalars(
        select(Company).where(or_(Company.coveted_tier <= 2, Company.coveted_pinned.is_not(None)))
    )).all()
    ids = [c.id for c in companies]
    counts = dict((await db.execute(
        select(Job.company_id, func.count()).where(Job.is_active.is_(True), Job.company_id.in_(ids))
        .group_by(Job.company_id)
    )).all()) if ids else {}
    targets = (await db.scalars(select(CovetedTarget).order_by(CovetedTarget.id))).all()
    by_company: dict[int, CovetedTarget] = {}
    for t in targets:
        if t.company_id is not None:
            by_company.setdefault(t.company_id, t)
    rows = []
    for c in companies:
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


@router.post("/pin", response_model=PinOut)
async def pin(body: PinIn, _: AdminUser, db: DbSession):
    """Pin a known company (tier 1 whatever its score), or add an employer we have no board for by name and
    domain; the latter is sent through the resolver right away."""
    if body.company_id is not None:
        c = await db.get(Company, body.company_id)
        if c is None:
            raise HTTPException(404, "No such company")
        c.coveted_pinned = True
        coveted.apply_score(c, c.coveted_score)
        await db.commit()
        return PinOut(company_id=c.id)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "Give the employer's name.")
    domain = coveted.clean_domain(body.domain or "")
    if domain is None:
        raise HTTPException(422, BAD_DOMAIN)
    t = await db.scalar(select(CovetedTarget).where(CovetedTarget.domain == domain))
    if t is None:
        t = CovetedTarget(name=name[:200], domain=domain, source="admin", score=100.0)
        db.add(t)
    else:  # an employer the audit already tracks: an admin now wants it too
        t.source, t.score = "admin", max(t.score, 100.0)
        if t.company_id is not None and (c := await db.get(Company, t.company_id)) is not None:
            c.coveted_pinned = True
            coveted.apply_score(c, c.coveted_score)
    await db.commit()
    task = None
    if t.company_id is None:
        task = await queue.enqueue(db, "audit_coverage", {"target_id": t.id}, dedupe=True)
    return PinOut(company_id=t.company_id, target_id=t.id, task_id=task.id if task else None)


@router.post("/unpin", response_model=PinOut)
async def unpin(body: UnpinIn, _: AdminUser, db: DbSession):
    """Undo a pin. For a company, `exclude` marks it not coveted at all; for a target with no board, the target
    is dropped."""
    if body.target_id is not None:
        t = await db.get(CovetedTarget, body.target_id)
        if t is None:
            raise HTTPException(404, "No such employer")
        await db.delete(t)
        await db.commit()
        return PinOut(target_id=body.target_id)
    if body.company_id is None:
        raise HTTPException(422, "Say which company or employer to unpin.")
    c = await db.get(Company, body.company_id)
    if c is None:
        raise HTTPException(404, "No such company")
    c.coveted_pinned = False if body.exclude else None
    coveted.apply_score(c, c.coveted_score)
    await db.commit()
    # an unpinned score is stale (an exclusion zeroes it): recompute without running a whole audit
    task = await queue.enqueue(db, "score_coveted", {"no_audit": True}, dedupe=True)
    return PinOut(company_id=c.id, task_id=task.id)


@router.post("/rescore", response_model=TaskRef, status_code=202)
async def rescore(_: AdminUser, db: DbSession):
    task = await queue.enqueue(db, "score_coveted", dedupe=True)
    return TaskRef(task_id=task.id)


@router.post("/targets/{target_id}/resolve", response_model=TaskRef, status_code=202)
async def resolve_target(target_id: int, _: AdminUser, db: DbSession):
    if await db.get(CovetedTarget, target_id) is None:
        raise HTTPException(404, "No such employer")
    task = await queue.enqueue(db, "audit_coverage", {"target_id": target_id}, dedupe=True)
    return TaskRef(task_id=task.id)


@router.post("/{company_id}/resolve", response_model=TaskRef, status_code=202)
async def resolve_company(company_id: int, _: AdminUser, db: DbSession):
    if await db.get(Company, company_id) is None:
        raise HTTPException(404, "No such company")
    task = await queue.enqueue(db, "audit_coverage", {"company_id": company_id}, dedupe=True)
    return TaskRef(task_id=task.id)
