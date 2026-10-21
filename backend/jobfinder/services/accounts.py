"""Accounts: signing in and out, password links (request and confirm), and the CLI's user commands.

Nothing here commits. The request (or CLI command) commits once; a failed sign-in's rate events go
through `side_session` so they count although the 401 rolls the request back.
"""
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jobfinder.core.config import settings
from jobfinder.core.db import AsyncSession, Sessions, side_session
from jobfinder.core.errors import BadRequest, Invalid, NotFound, RateLimited, Unauthorized
from jobfinder.core.urls import public_link
from jobfinder.integrations import smtp as mail
from jobfinder.models import User
from jobfinder.repositories import users
from jobfinder.services import ratelimit
from jobfinder.services.auth import (
    create_session,
    hash_password_async,
    hash_session_token,
    verify_or_dummy,
)
from jobfinder.services.tokens import (
    InvalidToken,
    make_password_token,
    password_fingerprint,
    read_password_token,
)

log = logging.getLogger("jobfinder.auth")

LINK_SENT = "If that address has access, we've emailed a link. It expires in 60 minutes."
BAD_LINK = "This link is invalid or has expired. Request a new one from the sign-in page."


class WrongCredentials(Unauthorized):
    code = "invalid_credentials"


class InvalidLink(BadRequest):
    code = "invalid_link"


@dataclass(frozen=True)
class PasswordLink:
    """A link to email after the request commits (the router hands it to a background task)."""
    email: str
    pending: bool  # never set a password: "Set" wording instead of "Reset"
    link: str


def _valid_zone(name: str | None) -> str:
    try:
        ZoneInfo(name or "UTC")
        return name or "UTC"
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"


def reset_link(user: User) -> str:
    token = make_password_token(user.id, user.password_hash)
    return public_link(settings.public_base_url, f"/reset-password#token={token}")


async def login(db: AsyncSession, sessions: Sessions, email: str, password: str, ip: str) -> tuple[User, str]:
    """The user and a new session token, or 401 (counted against the email and the IP) / 429."""
    s = settings
    email = email.lower()
    window = s.login_failure_window_minutes * 60
    if (
        await ratelimit.count_since(db, "login_fail_email", email, window) >= s.login_failures_per_email
        or await ratelimit.count_since(db, "login_fail_ip", ip, window) >= s.login_failures_per_ip
    ):
        raise RateLimited("Too many attempts. Try again later.")
    user = await users.by_email(db, email)
    ok = await verify_or_dummy(user.password_hash if user else None, password)
    if not user or not ok:
        async with side_session(sessions) as side:  # counts despite the 401 rollback
            await ratelimit.hit(side, "login_fail_email", email)
            await ratelimit.hit(side, "login_fail_ip", ip)
        raise WrongCredentials("Invalid email or password")
    return user, await create_session(db, user.id)


async def request_link(db: AsyncSession, email: str, ip: str) -> PasswordLink | None:
    """Counts the request; returns a link to email only for an existing address that isn't rate limited.
    The caller answers the same either way."""
    s = settings
    email = email.lower()
    limited = (
        await ratelimit.count_since(db, "link_ip", ip, 3600) >= s.link_requests_per_ip_hour
        or await ratelimit.count_since(db, "link_email", email, 3600) >= s.link_requests_per_email_hour
    )
    last = await ratelimit.last_at(db, "link_email", email)
    if last and datetime.now(UTC) - last < timedelta(seconds=s.link_cooldown_seconds):
        limited = True
    await ratelimit.hit(db, "link_ip", ip)
    await ratelimit.hit(db, "link_email", email)
    user = await users.by_email(db, email)
    if not user or limited:
        return None
    return PasswordLink(user.email, user.password_hash is None, reset_link(user))


async def send_link(link: PasswordLink) -> None:
    ttl = settings.password_link_ttl_minutes
    pending, url = link.pending, link.link
    subject = "Set your JobFinder password" if pending else "Reset your JobFinder password"
    text = f"Use this link to {'set' if pending else 'reset'} your password. It works once and expires in {ttl} minutes.\n\n{url}\n\nIf you didn't ask for this, ignore this email."
    html = f'<p>Use this link to {"set" if pending else "reset"} your password. It works once and expires in {ttl} minutes.</p><p><a href="{url}">{"Set" if pending else "Reset"} your password</a></p><p>If you didn\'t ask for this, ignore this email.</p>'
    try:
        await mail.send_email(link.email, subject, text, html)
    except Exception:
        log.exception("password link email failed")


async def confirm_reset(
    db: AsyncSession, token: str, password: str, timezone: str | None
) -> tuple[User, str]:
    """Sets the password (activating a pending account), ends every other session, and signs in."""
    try:
        uid, fp = read_password_token(token)
    except InvalidToken:
        raise InvalidLink(BAD_LINK) from None
    user = await users.get(db, uid)
    if not user or fp != password_fingerprint(user.password_hash):
        raise InvalidLink(BAD_LINK)
    if password.lower() == user.email.lower():
        raise Invalid("Password can't be your email address")
    pending = user.password_hash is None
    user.password_hash = await hash_password_async(password)
    if pending:
        user.timezone = _valid_zone(timezone)
        user.digest_hour, user.digest_enabled, user.question_emails_enabled = 8, True, True
        user.activated_at = datetime.now(UTC)
        await users.ensure_profile(db, user.id)
    await users.delete_sessions_of(db, user.id)
    return user, await create_session(db, user.id)


async def logout(db: AsyncSession, raw_token: str | None) -> None:
    if raw_token:
        await users.delete_session(db, hash_session_token(raw_token))


# The CLI's user commands (the command commits).

def normal_email(email: str) -> str:
    return email.strip().lower()


async def by_email(db: AsyncSession, email: str) -> User:
    """The user with this address (any case, surrounding spaces ignored), or NotFound."""
    user = await users.by_email(db, normal_email(email))
    if not user:
        raise NotFound(f"No user {email.strip()}")
    return user


async def list_users(db: AsyncSession) -> list[User]:
    return await users.all_by_id(db)


async def active_users(db: AsyncSession, email: str | None = None) -> list[User]:
    """Users who have set a password; with `email`, only that one (NotFound when there is no such user,
    Invalid when they haven't set one yet)."""
    if email:
        user = await by_email(db, email)
        if not user.password_hash:
            raise Invalid(f"{user.email} has not set a password yet")
        return [user]
    return await users.with_password(db)


async def add_user(db: AsyncSession, email: str) -> User | None:
    """A pending user (no password yet), or None when the address already exists."""
    email = normal_email(email)
    if await users.by_email(db, email):
        return None
    user = users.add(db, email)
    await db.flush()
    return user


async def link_for(db: AsyncSession, email: str) -> str:
    return reset_link(await by_email(db, email))


async def set_admin(db: AsyncSession, email: str, value: bool) -> int | None:
    """None when the user already had that value; otherwise how many admins there now are."""
    user = await by_email(db, email)
    if user.is_admin == value:
        return None
    user.is_admin = value
    await db.flush()
    return await users.count_admins(db)
