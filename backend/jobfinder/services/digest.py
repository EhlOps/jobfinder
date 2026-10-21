"""The daily email run: every user whose chosen hour has come gets their digest, with whatever questions
were collected for them. Collection runs on the `ai` queue ahead of the send hour (`schedule_questions`),
so sending never calls Claude (#50). The `daily_emails` task and `jobfinder digest send` both call this."""
import logging
from datetime import datetime, timedelta

from jobfinder.core.config import settings
from jobfinder.core.db import AsyncSession
from jobfinder.integrations.smtp import EmailError
from jobfinder.repositories import tasks as tasks_repo
from jobfinder.repositories import users
from jobfinder.services import daily
from jobfinder.tasks import ledger

log = logging.getLogger("jobfinder.digest")


async def schedule_questions(db: AsyncSession, now: datetime) -> int:
    """Queue `collect_questions` for each user whose digest is QUESTIONS_LEAD_HOURS or less away, once per
    digest: any collection (this one, the planner's, match_user's) due since the window opened stops a
    second. The row shares their dedupe key, so it never runs beside one of them. Flushes only. Returns
    how many were queued."""
    lead, queued = timedelta(hours=settings.questions_lead_hours), 0
    for user, digest_at in await daily.question_users(db, now):
        if await tasks_repo.any_since(db, user.id, "collect_questions", digest_at - lead):
            continue
        await ledger.add(db, "collect_questions", user_id=user.id, run_after=now, dedupe=True)
        queued += 1
    return queued


async def send_due(db: AsyncSession, now: datetime) -> dict:
    """Each user is its own boundary: their email is committed before the next user, so one user's crash
    rolls back only theirs. Returns counts plus per-user errors."""
    out = {"due": 0, "sent": 0, "nothing_to_send": 0, "errors": {}}
    for user in await daily.due_users(db, now):
        out["due"] += 1
        try:
            await users.refresh_if_expired(db, user)  # an earlier user's rollback expired the loaded rows
            result = await daily.send_daily(db, user, now=now)
            await db.commit()
            out["sent" if result else "nothing_to_send"] += 1
        except EmailError as e:
            log.warning("daily email for user %s failed: %s", user.id, e)
            out["errors"][str(user.id)] = str(e)[:200]
        except Exception as e:
            log.exception("daily email for user %s crashed", user.id)
            out["errors"][str(user.id)] = f"{type(e).__name__}: {e}"[:200]
            await db.rollback()
    return out
