"""The daily email run: every user whose chosen hour has come gets their digest (and, when they opted
in, freshly collected questions). The `daily_emails` task and `jobfinder digest send` both call this."""
import logging
from datetime import datetime

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.integrations.claude.claude_code import AIError
from jobfinder.integrations.smtp import EmailError
from jobfinder.notify import daily
from jobfinder.services import questions

log = logging.getLogger("jobfinder.digest")


async def send_due(db: AsyncSession, ai, now: datetime) -> dict:
    """Each user is its own boundary: their work is committed before the next user, so one user's crash
    rolls back only theirs. Returns counts plus per-user errors."""
    out = {"due": 0, "sent": 0, "nothing_to_send": 0, "errors": {}}
    for user in await daily.due_users(db, now):
        out["due"] += 1
        try:
            if inspect(user).expired_attributes:  # an earlier user's rollback expired the loaded rows
                await db.refresh(user)
            if user.question_emails_enabled:
                try:
                    await questions.collect_questions(db, ai, user.id)
                except AIError as e:  # still send the matches; questions go out tomorrow
                    log.warning("question collection for user %s failed: %s", user.id, e)
                # Commit before sending: send_daily rolls back when another run already claimed the email,
                # and the collected questions must survive that.
                await db.commit()
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
