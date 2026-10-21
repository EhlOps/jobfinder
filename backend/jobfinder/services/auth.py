"""Sign-in sessions and password checks. The cookie and the `CurrentUser` dependency are api/deps.py's."""
from datetime import UTC, datetime, timedelta

from anyio.to_thread import run_sync

from jobfinder.core.config import settings
from jobfinder.core.db import AsyncSession
from jobfinder.core.security import hash_password, new_token, verify_password
from jobfinder.core.security import hash_token as hash_session_token
from jobfinder.models import User
from jobfinder.repositories import users

SEEN_EVERY = timedelta(minutes=10)


async def hash_password_async(password: str) -> str:
    return await run_sync(hash_password, password)


_dummy_hash: str | None = None


async def verify_or_dummy(password_hash: str | None, password: str) -> bool:
    """Always does one argon2 verification (against a dummy hash when there is none) so timing doesn't
    reveal whether the account exists or has a password."""
    global _dummy_hash
    if password_hash is None:
        if _dummy_hash is None:
            _dummy_hash = hash_password("dummy-password-for-timing")
        await run_sync(verify_password, _dummy_hash, password)
        return False
    return await run_sync(verify_password, password_hash, password)


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


async def session_user(db: AsyncSession, token: str) -> User | None:
    """The user signed in with this session token, or None when it is unknown or expired."""
    now = datetime.now(UTC)
    user = await users.by_session(db, hash_session_token(token), now)
    if user and (user.last_seen_at is None or now - user.last_seen_at > SEEN_EVERY):
        user.last_seen_at = now  # the planner throttles idle users by this; the request commits it
    return user
