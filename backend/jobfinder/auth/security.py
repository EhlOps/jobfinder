from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from jobfinder.core.config import settings
from jobfinder.core.db import DbSession
from jobfinder.core.security import hash_password, new_token, verify_password
from jobfinder.core.security import hash_token as hash_session_token
from jobfinder.models import Session, User

COOKIE_NAME = "session"
SEEN_EVERY = timedelta(minutes=10)


async def hash_password_async(password: str) -> str:
    return await run_in_threadpool(hash_password, password)


_dummy_hash: str | None = None


async def verify_or_dummy(password_hash: str | None, password: str) -> bool:
    """Always does one argon2 verification (against a dummy hash when there is none) so timing doesn't
    reveal whether the account exists or has a password."""
    global _dummy_hash
    if password_hash is None:
        if _dummy_hash is None:
            _dummy_hash = hash_password("dummy-password-for-timing")
        await run_in_threadpool(verify_password, _dummy_hash, password)
        return False
    return await run_in_threadpool(verify_password, password_hash, password)


async def create_session(db: AsyncSession, user_id: int) -> str:
    """Creates the session and returns the raw token for the cookie (the database keeps only its hash)."""
    token = new_token()
    db.add(Session(
        token=hash_session_token(token), user_id=user_id,
        expires_at=datetime.now(UTC) + timedelta(days=settings.session_ttl_days),
    ))
    await db.commit()
    return token


async def prune_sessions(db: AsyncSession) -> None:
    await db.execute(delete(Session).where(Session.expires_at < datetime.now(UTC)))
    await db.commit()


async def current_user(
    db: DbSession,
    session: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> User:
    if session:
        row = (
            await db.execute(
                select(User)
                .join(Session, Session.user_id == User.id)
                .where(
                    Session.token == hash_session_token(session), Session.expires_at > datetime.now(UTC), User.password_hash.is_not(None)
                )
            )
        ).scalar_one_or_none()
        if row:
            now = datetime.now(UTC)
            if row.last_seen_at is None or now - row.last_seen_at > SEEN_EVERY:
                row.last_seen_at = now  # the planner throttles idle users by this
                await db.commit()
            return row
    raise HTTPException(401, "Not authenticated")


async def require_admin(user: Annotated[User, Depends(current_user)]) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Admin only")
    return user
