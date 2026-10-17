"""Clarifying questions, plus the match rows the questions flow reads and marks (unknowns, reviewed flag),
and the user row the unsubscribe link switches off (read through session.get; see the package note)."""
from collections.abc import Iterable

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import ClarifyingQuestion, Job, JobMatch, User

OPEN = ("pending", "emailed")


async def open_count(db: AsyncSession, user_id: int) -> int:
    return await db.scalar(
        sa.select(sa.func.count()).select_from(ClarifyingQuestion).where(
            ClarifyingQuestion.user_id == user_id, ClarifyingQuestion.status.in_(OPEN)
        )
    ) or 0


async def open_for(db: AsyncSession, user_id: int) -> list[ClarifyingQuestion]:
    return list(
        await db.scalars(
            sa.select(ClarifyingQuestion)
            .where(ClarifyingQuestion.user_id == user_id, ClarifyingQuestion.status.in_(OPEN))
            .order_by(ClarifyingQuestion.id)
        )
    )


async def open_by_ids(db: AsyncSession, user_id: int, ids: Iterable[int]) -> dict[int, ClarifyingQuestion]:
    """The user's still-open questions among `ids`, by id (foreign, unknown or closed ids are left out)."""
    rows = await db.scalars(
        sa.select(ClarifyingQuestion).where(
            ClarifyingQuestion.id.in_(list(ids)), ClarifyingQuestion.user_id == user_id, ClarifyingQuestion.status.in_(OPEN)
        )
    )
    return {q.id: q for q in rows}


async def asked(db: AsyncSession, user_id: int) -> list[tuple[str, str]]:
    """Every question ever asked of the user, with its status."""
    rows = await db.scalars(sa.select(ClarifyingQuestion).where(ClarifyingQuestion.user_id == user_id))
    return [(q.question, q.status) for q in rows]


def add(db: AsyncSession, question: ClarifyingQuestion) -> None:
    db.add(question)


async def unreviewed_matches(db: AsyncSession, user_id: int, limit: int) -> list[JobMatch]:
    """Live matches (new or saved, active job) whose unknowns haven't been turned into questions, best first."""
    return list(
        (
            await db.execute(
                sa.select(JobMatch)
                .join(Job, Job.id == JobMatch.job_id)
                .where(
                    JobMatch.user_id == user_id, JobMatch.questions_collected.is_(False),
                    JobMatch.status.in_(("new", "saved")), Job.is_active.is_(True),
                )
                .order_by(JobMatch.llm_score.desc())
                .limit(limit)
            )
        ).scalars()
    )


async def mark_reviewed(db: AsyncSession, match_ids: list[int]) -> None:
    await db.execute(sa.update(JobMatch).where(JobMatch.id.in_(match_ids)).values(questions_collected=True))


async def match_jobs(db: AsyncSession, user_id: int, match_ids: set[int]) -> dict[int, dict]:
    """Title and company of the user's matches among `match_ids`, in one query."""
    if not match_ids:
        return {}
    rows = await db.execute(
        sa.select(JobMatch.id, Job.title, Job.company_name)
        .join(Job, Job.id == JobMatch.job_id)
        .where(JobMatch.id.in_(match_ids), JobMatch.user_id == user_id)
    )
    return {mid: {"match_id": mid, "title": title, "company": company} for mid, title, company in rows.all()}


async def user(db: AsyncSession, user_id: int) -> User | None:
    return await db.get(User, user_id)
