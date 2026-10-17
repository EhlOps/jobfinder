"""Admin › Coverage: how sought-after each employer is, and the coveted ones we can't read yet."""
from fastapi import APIRouter

from jobfinder.api.deps import AdminUser, DbSession
from jobfinder.schemas.common import TaskRef
from jobfinder.schemas.coverage import Coverage, PinIn, PinOut, UnpinIn
from jobfinder.services import admin

router = APIRouter(prefix="/api/admin/coverage", tags=["coverage"])


@router.get("", response_model=Coverage)
async def coverage(_: AdminUser, db: DbSession):
    """Coveted employers (tier 1-2, or pinned/unpinned by an admin), most coveted first, plus unresolved targets."""
    return await admin.coverage_view(db)


@router.post("/pin", response_model=PinOut)
async def pin(body: PinIn, _: AdminUser, db: DbSession):
    """Pin a known company (tier 1 whatever its score), or add an employer we have no board for by name and
    domain; the latter is sent through the resolver right away."""
    return await admin.pin(db, body)


@router.post("/unpin", response_model=PinOut)
async def unpin(body: UnpinIn, _: AdminUser, db: DbSession):
    """Undo a pin. For a company, `exclude` marks it not coveted at all; for a target with no board, the target
    is dropped."""
    return await admin.unpin(db, body)


@router.post("/rescore", response_model=TaskRef, status_code=202)
async def rescore(_: AdminUser, db: DbSession):
    return TaskRef(task_id=await admin.rescore(db))


@router.post("/targets/{target_id}/resolve", response_model=TaskRef, status_code=202)
async def resolve_target(target_id: int, _: AdminUser, db: DbSession):
    return TaskRef(task_id=await admin.resolve_target(db, target_id))


@router.post("/{company_id}/resolve", response_model=TaskRef, status_code=202)
async def resolve_company(company_id: int, _: AdminUser, db: DbSession):
    return TaskRef(task_id=await admin.resolve_company(db, company_id))
