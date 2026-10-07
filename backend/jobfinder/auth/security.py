import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Cookie, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from jobfinder.config import get_settings
from jobfinder.db import get_db
from jobfinder.models import Session, User

COOKIE_NAME = "session"
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


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


def hash_session_token(token: str) -> str:
    """Only this digest is stored, so a leaked sessions table can't be used to sign in. The token is
    256 bits of randomness, so a plain SHA-256 is enough (no salt or slow hash needed)."""
    return hashlib.sha256(token.encode()).hexdigest()


async def create_session(db: AsyncSession, user_id: int) -> str:
    """Creates the session and returns the raw token for the cookie (the database keeps only its hash)."""
    token = secrets.token_urlsafe(32)
    db.add(Session(
        token=hash_session_token(token), user_id=user_id,
        expires_at=datetime.now(UTC) + timedelta(days=get_settings().session_ttl_days),
    ))
    await db.commit()
    return token


async def prune_sessions(db: AsyncSession) -> None:
    await db.execute(delete(Session).where(Session.expires_at < datetime.now(UTC)))
    await db.commit()


async def current_user(
    db: Annotated[AsyncSession, Depends(get_db)],
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
            row.is_admin = is_admin_email(row.email)  # ADMIN_EMAILS is the only source of admin
            return row
    raise HTTPException(401, "Not authenticated")


async def require_admin(user: Annotated[User, Depends(current_user)]) -> User:
    if not is_admin_email(user.email):
        raise HTTPException(403, "Admin only")
    return user


def is_admin_email(email: str) -> bool:
    return email.lower() in {e.strip().lower() for e in get_settings().admin_emails.split(",") if e.strip()}
