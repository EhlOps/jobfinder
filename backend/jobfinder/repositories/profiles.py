"""Profiles and profile facts: the rows the profile, onboarding and interview services read and write.

Also the one read the interview needs from matches (their open requirements); see the package note.
"""
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import Job, JobMatch, Profile, ProfileFact


async def get(db: AsyncSession, user_id: int, *, for_update: bool = False) -> Profile | None:
    return await db.get(Profile, user_id, with_for_update=for_update)


async def create(db: AsyncSession, user_id: int) -> Profile:
    profile = Profile(user_id=user_id, status={}, background={}, version=1)
    db.add(profile)
    await db.flush()
    return profile


async def lock(db: AsyncSession, profile: Profile) -> None:
    """Re-read the row under FOR UPDATE; the lock lasts until the caller's transaction ends."""
    await db.refresh(profile, with_for_update=True)


async def facts(db: AsyncSession, user_id: int) -> list[ProfileFact]:
    return list(await db.scalars(sa.select(ProfileFact).where(ProfileFact.user_id == user_id).order_by(ProfileFact.id)))


async def fact_questions(db: AsyncSession, user_id: int) -> list[str]:
    return list(await db.scalars(sa.select(ProfileFact.question).where(ProfileFact.user_id == user_id)))


async def get_fact(db: AsyncSession, fact_id: int) -> ProfileFact | None:
    return await db.get(ProfileFact, fact_id)


async def add_fact(db: AsyncSession, fact: ProfileFact) -> ProfileFact:
    """Add one fact and flush it, so it has its id."""
    db.add(fact)
    await db.flush()
    return fact


def add_facts(db: AsyncSession, facts: list[ProfileFact]) -> None:
    """Stage several facts; the next flush or the boundary's commit writes them in one go."""
    db.add_all(facts)


async def delete_fact(db: AsyncSession, fact: ProfileFact) -> None:
    await db.delete(fact)
    await db.flush()


async def open_requirements(db: AsyncSession, user_id: int) -> list[list[dict] | None]:
    """The requirements lists of the user's live matches (new or saved, on active jobs)."""
    return list(
        await db.scalars(
            sa.select(JobMatch.requirements)
            .join(Job, Job.id == JobMatch.job_id)
            .where(JobMatch.user_id == user_id, JobMatch.status.in_(("new", "saved")), Job.is_active.is_(True))
        )
    )
