from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.api.deps import CurrentUser, DbSession
from jobfinder.models import Profile, ProfileFact, User
from jobfinder.schemas.profile import FactIn, FactOut, ProfileOut, Status

router = APIRouter(prefix="/api/profile", tags=["profile"])


async def get_profile(db: AsyncSession, user: User) -> Profile:
    profile = await db.get(Profile, user.id)
    if profile is None:
        profile = Profile(user_id=user.id, status={}, background={}, version=1)
        db.add(profile)
        await db.flush()
    return profile


@router.get("", response_model=ProfileOut)
async def read_profile(
    user: CurrentUser, db: DbSession
):
    profile = await get_profile(db, user)
    await db.commit()
    return profile


@router.put("/status", response_model=ProfileOut)
async def put_status(
    body: Status,
    user: CurrentUser,
    db: DbSession,
):
    profile = await get_profile(db, user)
    profile.status = body.model_dump()
    profile.version += 1
    await db.commit()
    return profile


@router.put("/background", response_model=ProfileOut)
async def put_background(
    body: dict[str, Any],
    user: CurrentUser,
    db: DbSession,
):
    """Free-form for now; milestone 3 defines the extracted schema."""
    profile = await get_profile(db, user)
    profile.background = body
    profile.version += 1
    await db.commit()
    return profile


@router.get("/facts", response_model=list[FactOut])
async def list_facts(
    user: CurrentUser, db: DbSession
):
    rows = await db.execute(
        select(ProfileFact).where(ProfileFact.user_id == user.id).order_by(ProfileFact.id)
    )
    return rows.scalars().all()


@router.post("/facts", response_model=FactOut, status_code=201)
async def add_fact(
    body: FactIn,
    user: CurrentUser,
    db: DbSession,
):
    fact = ProfileFact(user_id=user.id, **body.model_dump())
    db.add(fact)
    (await get_profile(db, user)).version += 1
    await db.commit()
    return fact


@router.delete("/facts/{fact_id}", status_code=204)
async def delete_fact(
    fact_id: int,
    user: CurrentUser,
    db: DbSession,
):
    fact = await db.get(ProfileFact, fact_id)
    if not fact or fact.user_id != user.id:
        raise HTTPException(404, "Not found")
    await db.delete(fact)
    (await get_profile(db, user)).version += 1
    await db.commit()
