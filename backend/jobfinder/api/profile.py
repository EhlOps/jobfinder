"""The profile: job-search status, background, and the facts the user added."""
from typing import Any

from fastapi import APIRouter

from jobfinder.api.deps import CurrentUser, DbSession
from jobfinder.schemas.profile import FactIn, FactOut, ProfileOut, Status
from jobfinder.services import profile as service

router = APIRouter(prefix="/api/profile", tags=["profile"])


@router.get("", response_model=ProfileOut)
async def read_profile(user: CurrentUser, db: DbSession):
    return await service.get_or_create(db, user.id)


@router.put("/status", response_model=ProfileOut)
async def put_status(body: Status, user: CurrentUser, db: DbSession):
    return await service.set_status(db, user.id, body.model_dump())


@router.put("/background", response_model=ProfileOut)
async def put_background(body: dict[str, Any], user: CurrentUser, db: DbSession):
    """Free-form for now; milestone 3 defines the extracted schema."""
    return await service.set_background(db, user.id, body)


@router.get("/facts", response_model=list[FactOut])
async def list_facts(user: CurrentUser, db: DbSession):
    return await service.list_facts(db, user.id)


@router.post("/facts", response_model=FactOut, status_code=201)
async def add_fact(body: FactIn, user: CurrentUser, db: DbSession):
    return await service.add_fact(db, user.id, body.model_dump())


@router.delete("/facts/{fact_id}", status_code=204)
async def delete_fact(fact_id: int, user: CurrentUser, db: DbSession):
    await service.delete_fact(db, user.id, fact_id)
