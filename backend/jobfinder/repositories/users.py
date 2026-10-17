"""Users and their sign-in sessions (the sessions table holds only token hashes)."""
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import Profile, Session, User


async def get(db: AsyncSession, user_id: int) -> User | None:
    return await db.get(User, user_id)


async def by_email(db: AsyncSession, email: str) -> User | None:
    return (await db.execute(sa.select(User).where(User.email == email))).scalar_one_or_none()


async def by_session(db: AsyncSession, token_hash: str, now: datetime) -> User | None:
    """The user behind an unexpired session, if they have set a password (pending rows can't sign in)."""
    return (
        await db.execute(
            sa.select(User)
            .join(Session, Session.user_id == User.id)
            .where(Session.token == token_hash, Session.expires_at > now, User.password_hash.is_not(None))
        )
    ).scalar_one_or_none()


def add(db: AsyncSession, email: str) -> User:
    user = User(email=email)
    db.add(user)
    return user


async def count_admins(db: AsyncSession) -> int:
    return await db.scalar(sa.select(sa.func.count()).select_from(User).where(User.is_admin.is_(True))) or 0


async def profile_of(db: AsyncSession, user_id: int) -> Profile | None:
    return await db.get(Profile, user_id)


async def ensure_profile(db: AsyncSession, user_id: int) -> None:
    """An empty profile for a newly activated user, unless one exists."""
    if not await db.get(Profile, user_id):
        db.add(Profile(user_id=user_id, status={}, background={}, version=1))


def add_session(db: AsyncSession, token_hash: str, user_id: int, expires_at: datetime) -> None:
    db.add(Session(token=token_hash, user_id=user_id, expires_at=expires_at))


async def delete_session(db: AsyncSession, token_hash: str) -> None:
    await db.execute(sa.delete(Session).where(Session.token == token_hash))


async def delete_sessions_of(db: AsyncSession, user_id: int) -> None:
    await db.execute(sa.delete(Session).where(Session.user_id == user_id))


async def delete_expired_sessions(db: AsyncSession, now: datetime) -> None:
    await db.execute(sa.delete(Session).where(Session.expires_at < now))
