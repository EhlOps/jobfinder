"""Match reads and the feed queries: one user's matches, each with its job (and the job's company).

`get_owned` is the one ownership check for everything under /api/matches/{id} and for the letter
and resume workers: a match id that isn't the user's reads as missing.
"""
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.core.text import early_career_pattern
from jobfinder.models import ClarifyingQuestion, Company, Job, JobMatch, ProfileFact

STATUSES = ("new", "saved", "applied", "dismissed")
EARLY_CAREER_SQL = early_career_pattern("postgres")  # the prefilter's EARLY_CAREER_TITLE, for Postgres


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
            Job.seniority.in_(("intern", "new_grad")), Job.title.op("~*")(EARLY_CAREER_SQL),
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


# ── scoring (services/matching/service.py) ───────────────────────────────
async def scored_since(db: AsyncSession, user_id: int, since: datetime) -> int:
    """How many of the user's matches were scored at or after `since` (the daily budget's window)."""
    return await db.scalar(
        sa.select(sa.func.count()).select_from(JobMatch).where(JobMatch.user_id == user_id, JobMatch.scored_at >= since)
    ) or 0


async def unmatched_jobs(db: AsyncSession, user_id: int, seniorities: set[str] | None) -> list:
    """Light rows (no description) for the active jobs not yet matched for the user, by id or by posting
    (dedupe_hash); with `seniorities`, only jobs at one of them or of unknown seniority."""
    matched_jobs = sa.select(JobMatch.job_id).where(JobMatch.user_id == user_id)
    matched_hashes = (
        sa.select(Job.dedupe_hash).join(JobMatch, JobMatch.job_id == Job.id).where(JobMatch.user_id == user_id)
    )
    q = (
        sa.select(
            Job.id, Job.title, Job.location, Job.workplace_type, Job.salary_max, Job.salary_currency,
            Job.posted_at, Job.dedupe_hash, Job.seniority,
            Company.prestige_tier.label("prestige_tier"), Company.size.label("size"), Company.industry.label("industry"),
            Company.sponsors_visas.label("sponsors_visas"),
        )
        .outerjoin(Company, Company.id == Job.company_id)
        .where(Job.is_active.is_(True), Job.id.not_in(matched_jobs), Job.dedupe_hash.not_in(matched_hashes))
    )
    if seniorities is not None:
        q = q.where(sa.or_(Job.seniority.is_(None), Job.seniority.in_(seniorities)))
    return list((await db.execute(q)).all())


async def descriptions(db: AsyncSession, job_ids: list[int]) -> dict[int, str]:
    return dict((await db.execute(sa.select(Job.id, Job.description_text).where(Job.id.in_(job_ids)))).all())


async def jobs_with_companies(db: AsyncSession, job_ids: list[int]) -> list[tuple[Job, Company | None]]:
    rows = await db.execute(
        sa.select(Job, Company).outerjoin(Company, Company.id == Job.company_id).where(Job.id.in_(job_ids))
    )
    return [(j, c) for j, c in rows.all()]


async def answered_match_ids(db: AsyncSession, user_id: int) -> set[int]:
    """Matches linked to clarifying questions the user has answered, and matches for jobs the user answered
    a question about directly on the job page."""
    rows = await db.scalars(
        sa.select(ClarifyingQuestion.match_ids).where(
            ClarifyingQuestion.user_id == user_id, ClarifyingQuestion.status == "answered"
        )
    )
    ids = {mid for found in rows for mid in found or []}
    ids |= set(
        await db.scalars(
            sa.select(JobMatch.id)
            .join(ProfileFact, ProfileFact.job_id == JobMatch.job_id)
            .where(JobMatch.user_id == user_id, ProfileFact.user_id == user_id, ProfileFact.source == "job_question")
        )
    )
    return ids


async def stale(db: AsyncSession, user_id: int, version: int, statuses: tuple[str, ...]) -> list:
    """Light rows (no description) for the matches at one of `statuses`, scored against a profile older than
    `version`, on active jobs. Unordered: services.matching ranks them with the new candidates."""
    rows = await db.execute(
        sa.select(
            JobMatch.id.label("match_id"), JobMatch.job_id, JobMatch.status, JobMatch.prefilter_score,
            Job.posted_at, Job.dedupe_hash,
        )
        .join(Job, Job.id == JobMatch.job_id)
        .where(
            JobMatch.user_id == user_id, JobMatch.profile_version < version,
            JobMatch.status.in_(statuses), Job.is_active.is_(True),
        )
    )
    return list(rows.all())


async def upsert(db: AsyncSession, values: dict) -> None:
    """Insert the match's assessment, or update it in place, leaving its status as it is."""
    stmt = insert(JobMatch).values(**values)
    await db.execute(
        stmt.on_conflict_do_update(
            constraint="uq_match_user_job", set_={k: stmt.excluded[k] for k in values if k not in ("user_id", "job_id")}
        )
    )
