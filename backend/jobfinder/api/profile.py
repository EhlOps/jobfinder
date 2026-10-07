from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.auth.security import current_user
from jobfinder.db import get_db
from jobfinder.models import Profile, ProfileFact, User

router = APIRouter(prefix="/api/profile", tags=["profile"])


class Status(BaseModel):
    """Where the person is in their job search and what they want."""

    is_new_grad: bool | None = None
    graduation_date: str | None = None  # YYYY-MM
    need_job_by: str | None = None  # YYYY-MM-DD
    target_roles: list[str] = []
    seniority: list[str] = []  # intern|new_grad|junior|mid|senior|staff
    target_locations: list[str] = []
    remote_preference: str | None = None  # remote|hybrid|onsite|any
    willing_to_relocate: bool | None = None
    needs_visa_sponsorship: bool | None = None
    salary_min: int | None = None
    salary_target: int | None = None
    prestige_preference: int | None = Field(default=None, ge=1, le=5)  # 1 = don't care, 5 = top-tier only
    company_sizes: list[str] = []  # startup|mid|large
    industries: list[str] = []
    notes: str | None = None


class ProfileOut(BaseModel):
    status: dict[str, Any]
    background: dict[str, Any]
    version: int

    model_config = {"from_attributes": True}


class FactIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=5000)
    source: str = "onboarding"


class FactOut(FactIn):
    id: int

    model_config = {"from_attributes": True}


async def get_profile(db: AsyncSession, user: User) -> Profile:
    profile = await db.get(Profile, user.id)
    if profile is None:
        profile = Profile(user_id=user.id, status={}, background={}, version=1)
        db.add(profile)
        await db.flush()
    return profile


@router.get("", response_model=ProfileOut)
async def read_profile(
    user: Annotated[User, Depends(current_user)], db: Annotated[AsyncSession, Depends(get_db)]
):
    profile = await get_profile(db, user)
    await db.commit()
    return profile


@router.put("/status", response_model=ProfileOut)
async def put_status(
    body: Status,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    profile = await get_profile(db, user)
    profile.status = body.model_dump()
    profile.version += 1
    await db.commit()
    return profile


@router.put("/background", response_model=ProfileOut)
async def put_background(
    body: dict[str, Any],
    user: Annotated[User, Depends(current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Free-form for now; milestone 3 defines the extracted schema."""
    profile = await get_profile(db, user)
    profile.background = body
    profile.version += 1
    await db.commit()
    return profile


@router.get("/facts", response_model=list[FactOut])
async def list_facts(
    user: Annotated[User, Depends(current_user)], db: Annotated[AsyncSession, Depends(get_db)]
):
    rows = await db.execute(
        select(ProfileFact).where(ProfileFact.user_id == user.id).order_by(ProfileFact.id)
    )
    return rows.scalars().all()


@router.post("/facts", response_model=FactOut, status_code=201)
async def add_fact(
    body: FactIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    fact = ProfileFact(user_id=user.id, **body.model_dump())
    db.add(fact)
    (await get_profile(db, user)).version += 1
    await db.commit()
    return fact


@router.delete("/facts/{fact_id}", status_code=204)
async def delete_fact(
    fact_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    fact = await db.get(ProfileFact, fact_id)
    if not fact or fact.user_id != user.id:
        raise HTTPException(404, "Not found")
    await db.delete(fact)
    (await get_profile(db, user)).version += 1
    await db.commit()
