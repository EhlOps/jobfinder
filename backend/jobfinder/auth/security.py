from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Cookie, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from jobfinder.core.config import settings
from jobfinder.core.db import DbSession
from jobfinder.core.errors import Forbidden, Unauthorized
from jobfinder.core.security import hash_password, new_token, verify_password
from jobfinder.core.security import hash_token as hash_session_token
from jobfinder.models import User
from jobfinder.repositories import users

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
    """Creates the session and returns the raw token for the cookie (the database keeps only its hash).
    Flushes; the caller's boundary commits."""
    token = new_token()
    expires = datetime.now(UTC) + timedelta(days=settings.session_ttl_days)
    users.add_session(db, hash_session_token(token), user_id, expires)
    await db.flush()
    return token


async def prune_sessions(db: AsyncSession) -> None:
    """Deletes expired sessions; the caller commits (the worker's maintenance tick)."""
    await users.delete_expired_sessions(db, datetime.now(UTC))


async def current_user(
    db: DbSession,
    session: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> User:
    if session:
        now = datetime.now(UTC)
        user = await users.by_session(db, hash_session_token(session), now)
        if user:
            if user.last_seen_at is None or now - user.last_seen_at > SEEN_EVERY:
                user.last_seen_at = now  # the planner throttles idle users by this; the request commits it
            return user
    raise Unauthorized("Not authenticated")


async def require_admin(user: Annotated[User, Depends(current_user)]) -> User:
    if not user.is_admin:
        raise Forbidden("Admin only")
    return user
