"""The daily email's reads and writes (services/daily.py): who gets one, what goes in it, and the claim
that marks it sent before the SMTP call (and gives it back when the send fails)."""
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import ClarifyingQuestion, Job, JobMatch, User


async def new_matches(db: AsyncSession, user_id: int, min_score: int, *, undigested_only: bool) -> list[tuple[JobMatch, Job]]:
    """The user's new matches on active jobs scoring at least `min_score`, best first."""
    q = (
        sa.select(JobMatch, Job)
        .join(Job, Job.id == JobMatch.job_id)
        .where(
            JobMatch.user_id == user_id, JobMatch.status == "new", Job.is_active.is_(True),
            JobMatch.llm_score >= min_score,
        )
        .order_by(JobMatch.llm_score.desc(), JobMatch.id.desc())
    )
    if undigested_only:
        q = q.where(JobMatch.digested_at.is_(None))
    return [(m, j) for m, j in (await db.execute(q)).all()]


async def questions(db: AsyncSession, user_id: int, statuses: Sequence[str], limit: int) -> list[ClarifyingQuestion]:
    q = (
        sa.select(ClarifyingQuestion)
        .where(ClarifyingQuestion.user_id == user_id, ClarifyingQuestion.status.in_(statuses))
        .order_by(ClarifyingQuestion.id).limit(limit)
    )
    return list((await db.scalars(q)).all())


async def claim_user(db: AsyncSession, user_id: int, prev: datetime | None, now: datetime) -> bool:
    """Compare-and-set last_digest_at from `prev` to `now`; False when another run got there first."""
    won = await db.execute(
        sa.update(User).where(User.id == user_id, User.last_digest_at.is_(None) if prev is None else User.last_digest_at == prev)
        .values(last_digest_at=now)
    )
    return bool(won.rowcount)


async def set_last_digest(db: AsyncSession, user_id: int, at: datetime | None) -> None:
    await db.execute(sa.update(User).where(User.id == user_id).values(last_digest_at=at))


async def mark_digested(db: AsyncSession, match_ids: list[int], at: datetime | None) -> None:
    await db.execute(sa.update(JobMatch).where(JobMatch.id.in_(match_ids)).values(digested_at=at))


async def mark_questions(db: AsyncSession, question_ids: list[int], status: str, emailed_at: datetime | None) -> None:
    await db.execute(
        sa.update(ClarifyingQuestion).where(ClarifyingQuestion.id.in_(question_ids))
        .values(status=status, emailed_at=emailed_at)
    )


async def _email_users(db: AsyncSession, *where) -> list[User]:
    """Active users with a chosen hour, narrowed by `where`."""
    return list((await db.scalars(sa.select(User).where(
        User.password_hash.is_not(None), User.digest_hour.is_not(None), *where,
    ))).all())


async def email_users(db: AsyncSession) -> list[User]:
    """Users who get a daily email of some kind (digest or questions)."""
    return await _email_users(db, sa.or_(User.digest_enabled.is_(True), User.question_emails_enabled.is_(True)))


async def question_email_users(db: AsyncSession) -> list[User]:
    """Users with question emails on and matches on active jobs still to review for questions."""
    unreviewed = sa.exists().where(
        JobMatch.user_id == User.id, JobMatch.questions_collected.is_(False),
        JobMatch.status.in_(("new", "saved")), JobMatch.job_id == Job.id, Job.is_active.is_(True),
    )
    return await _email_users(db, User.question_emails_enabled.is_(True), unreviewed)
