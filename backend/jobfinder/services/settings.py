"""The user's email and match-budget settings, and the preview of the daily email.

Nothing here commits except the preview's rate-limit event (a side session); the request commits once.
"""

from jobfinder.core.config import settings
from jobfinder.core.db import AsyncSession, Sessions, side_session
from jobfinder.core.errors import BadGateway, Conflict, RateLimited
from jobfinder.integrations.smtp import EmailError
from jobfinder.models import User
from jobfinder.notify import daily
from jobfinder.repositories import users
from jobfinder.schemas.settings import PreviewResult, SettingsBody
from jobfinder.services import ratelimit
from jobfinder.tasks import ledger


class NothingToPreview(Conflict):
    code = "nothing_to_preview"


def read(u: User) -> SettingsBody:
    return SettingsBody(
        digest_enabled=bool(u.digest_enabled), digest_hour=8 if u.digest_hour is None else u.digest_hour,
        timezone=u.timezone or "UTC", question_emails_enabled=bool(u.question_emails_enabled),
        match_budget_enabled=u.match_budget_enabled is not False,
        match_budget=u.match_budget if u.match_budget is not None else settings.match_daily_llm_budget,
    )


async def write(db: AsyncSession, user: User, body: SettingsBody) -> SettingsBody:
    user.digest_enabled, user.digest_hour = body.digest_enabled, body.digest_hour
    user.timezone, user.question_emails_enabled = body.timezone, body.question_emails_enabled
    before = read(user)
    user.match_budget_enabled = body.match_budget_enabled
    if body.match_budget is not None:
        user.match_budget = body.match_budget
    after = read(user)
    # More room than before: start scoring the waiting jobs now rather than at the next scheduled run.
    roomier = after.match_budget_enabled and (
        not before.match_budget_enabled or after.match_budget > before.match_budget
    )
    profile = await users.profile_of(db, user.id) if roomier else None
    if profile and profile.status:
        await ledger.add(db, "match_user", user_id=user.id, dedupe=True)
    return after


async def send_preview(db: AsyncSession, sessions: Sessions, user: User) -> PreviewResult:
    """Email the user what the daily email looks like right now (marks nothing as sent)."""
    if await ratelimit.count_since(db, "preview_email", str(user.id), 3600) >= settings.preview_emails_per_hour:
        raise RateLimited("Too many preview emails. Try again later.")
    async with side_session(sessions) as side:  # a failed send still counts against the limit
        await ratelimit.hit(side, "preview_email", str(user.id))
    try:
        result = await daily.send_daily(db, user, preview=True)
    except EmailError as e:
        raise BadGateway(f"Couldn't send the email: {e}") from None
    if result is None:
        raise NothingToPreview("Nothing to put in an email yet: no matches scoring 50+ or open questions.")
    return PreviewResult(to=user.email, matches=result["matches"], questions=result["questions"])
