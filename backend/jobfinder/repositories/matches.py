"""Match reads and the feed queries: one user's matches, each with its job (and the job's company).

`get_owned` is the one ownership check for everything under /api/matches/{id} and for the letter
and resume workers: a match id that isn't the user's reads as missing.
"""
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.matching import prefilter
from jobfinder.models import Company, Job, JobMatch, ProfileFact

STATUSES = ("new", "saved", "applied", "dismissed")


@dataclass(frozen=True)
class FeedQuery:
    status: str | None = None  # None: new + saved
    min_score: int = 0
    workplace: str | None = None
    q: str | None = None
    early_career: bool = False
    sort: str = "score"  # or "recent"
    limit: int = 30
    offset: int = 0


async def get_owned(db: AsyncSession, user_id: int, match_id: int) -> tuple[JobMatch, Job, Company | None] | None:
    """The user's match with its job and the job's company (None when the job has none)."""
    row = (
        await db.execute(
            sa.select(JobMatch, Job, Company)
            .join(Job, Job.id == JobMatch.job_id)
            .outerjoin(Company, Company.id == Job.company_id)
            .where(JobMatch.id == match_id, JobMatch.user_id == user_id)
        )
    ).first()
    return (row[0], row[1], row[2]) if row else None


def _visible(user_id: int):
    """Closed jobs disappear from the feed unless the user saved or applied to them."""
    return sa.and_(
        JobMatch.user_id == user_id,
        sa.or_(Job.is_active.is_(True), JobMatch.status.in_(("saved", "applied"))),
    )


def _filters(f: FeedQuery) -> list:
    out = [JobMatch.llm_score >= f.min_score]
    if f.workplace:
        out.append(Job.workplace_type == f.workplace)
    if f.early_career:
        out.append(sa.or_(
            Job.seniority.in_(("intern", "new_grad")), Job.title.op("~*")(prefilter.EARLY_CAREER_SQL),
            Job.title.op("~*")(r"\y(intern|internship|co-?op)\y"),
        ))
    if f.q:
        like = f"%{f.q.strip()}%"
        out.append(sa.or_(Job.title.ilike(like), Job.company_name.ilike(like)))
    return out


async def feed(
    db: AsyncSession, user_id: int, f: FeedQuery,
) -> tuple[dict[str, int], int, list[tuple[JobMatch, Job, bool | None]]]:
    """(tab counts, total in the chosen tab, one page of (match, job, the company's sponsors_visas)).

    Three queries whatever the page size: the company flag comes in on the page's own join."""
    filters = _filters(f)
    # Tab counts honour every filter except the status tab itself.
    counts = dict.fromkeys(STATUSES, 0)
    rows = await db.execute(
        sa.select(JobMatch.status, sa.func.count()).join(Job, Job.id == JobMatch.job_id)
        .where(_visible(user_id), *filters).group_by(JobMatch.status)
    )
    counts.update({s: n for s, n in rows.all()})

    filters.append(JobMatch.status == f.status if f.status else JobMatch.status.in_(("new", "saved")))
    total = await db.scalar(
        sa.select(sa.func.count()).select_from(JobMatch).join(Job, Job.id == JobMatch.job_id)
        .where(_visible(user_id), *filters)
    )
    order = (
        [JobMatch.llm_score.desc(), JobMatch.id.desc()] if f.sort == "score"
        else [sa.func.coalesce(Job.posted_at, JobMatch.scored_at).desc(), JobMatch.id.desc()]
    )
    page = await db.execute(
        sa.select(JobMatch, Job, Company.sponsors_visas)
        .join(Job, Job.id == JobMatch.job_id)
        .outerjoin(Company, Company.id == Job.company_id)
        .where(_visible(user_id), *filters).order_by(*order).limit(f.limit).offset(f.offset)
    )
    return counts, total or 0, [(m, j, sponsors) for m, j, sponsors in page.all()]


async def facts(db: AsyncSession, user_id: int) -> list[tuple[str, str]]:
    """The user's profile facts as (question, answer), oldest first: what letters and resumes draw on."""
    rows = await db.scalars(sa.select(ProfileFact).where(ProfileFact.user_id == user_id).order_by(ProfileFact.id))
    return [(f.question, f.answer) for f in rows]


def add_job_answers(db: AsyncSession, user_id: int, job_id: int, answers: list[tuple[str, str]]) -> None:
    """Answers given on a job's page become profile facts linked to that job."""
    db.add_all([
        ProfileFact(user_id=user_id, question=q, answer=a, source="job_question", job_id=job_id) for q, a in answers
    ])
