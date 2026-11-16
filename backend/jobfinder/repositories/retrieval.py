"""Embedding retrieval (#126): a profile version's nearest unmatched postings, the labels each Claude score
leaves behind (`match_reviews`), and the reads `jobfinder ops eval-retrieval` compares the engines with.

Only reads `job_embeddings` and `profile_embeddings`; the indexing tasks write them.
"""
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import Company, Job, JobMatch
from jobfinder.models.embedding import JobEmbedding, MatchReview, ProfileEmbedding
from jobfinder.repositories.matches import candidate_filter, light_columns

EF_SEARCH_MIN, EF_SEARCH_MAX = 100, 1000  # pgvector caps hnsw.ef_search at 1000


async def has_query_vectors(db: AsyncSession, user_id: int, version: int, model: str) -> bool:
    """Whether this profile version has been embedded with `model`."""
    return bool(await db.scalar(sa.select(sa.exists().where(
        ProfileEmbedding.user_id == user_id, ProfileEmbedding.profile_version == version,
        ProfileEmbedding.model == model,
    ))))


async def nearest_unmatched_jobs(
    db: AsyncSession, user_id: int, profile_version: int, model: str, k: int, acceptable_seniorities: set[str] | None,
    *, include_matched: bool = False,
) -> list:
    """For each of the profile version's query vectors, the `k` nearest postings embedded with the same model
    that `unmatched_jobs` would also return (active, not matched by id or posting, an acceptable seniority);
    the union, each job once with its best cosine as `retrieval_score`, nearest first. Light rows, the shape
    `unmatched_jobs` returns plus `retrieval_score`.

    The HNSW scan runs with iterative_scan (filtered-out rows don't shrink the result below k) in relaxed
    order, so the outer query sorts again by the exact distance. Both settings are LOCAL: they end with the
    transaction."""
    ef = min(max(k, EF_SEARCH_MIN), EF_SEARCH_MAX)
    await db.execute(sa.select(
        sa.func.set_config("hnsw.iterative_scan", "relaxed_order", True),
        sa.func.set_config("hnsw.ef_search", str(ef), True),
    ))
    query = ProfileEmbedding
    dist = JobEmbedding.embedding.cosine_distance(query.embedding)
    nearest = (
        sa.select(JobEmbedding.job_id, dist.label("dist"))
        .join(Job, Job.id == JobEmbedding.job_id)
        .where(JobEmbedding.model == model,
               candidate_filter(user_id, acceptable_seniorities, include_matched=include_matched))
        .order_by(dist)
        .limit(k)
        .lateral("nearest")
    )
    best = (
        sa.select(nearest.c.job_id, sa.func.min(nearest.c.dist).label("dist"))
        .select_from(query)
        .join(nearest, sa.true())
        .where(query.user_id == user_id, query.profile_version == profile_version, query.model == model)
        .group_by(nearest.c.job_id)
        .subquery("best")
    )
    rows = await db.execute(
        sa.select(*light_columns(), (1 - best.c.dist).label("retrieval_score"))
        .join(best, best.c.job_id == Job.id)
        .outerjoin(Company, Company.id == Job.company_id)
        .order_by(best.c.dist, Job.id)
    )
    return list(rows.all())


async def add_review(db: AsyncSession, values: dict) -> None:
    """One label per Claude score (append-only)."""
    await db.execute(sa.insert(MatchReview).values(**values))


# ── jobfinder ops eval-retrieval (read-only) ─────────────────────────────
async def users_with_scored_matches(db: AsyncSession) -> list[int]:
    return list(await db.scalars(sa.select(JobMatch.user_id).distinct().order_by(JobMatch.user_id)))


async def scored_postings(db: AsyncSession, user_id: int, profile_version: int) -> list:
    """The user's Claude scores against this profile version as (job_id, dedupe_hash, llm_score, verdict),
    best first."""
    rows = await db.execute(
        sa.select(JobMatch.job_id, Job.dedupe_hash, JobMatch.llm_score, JobMatch.verdict)
        .join(Job, Job.id == JobMatch.job_id)
        .where(JobMatch.user_id == user_id, JobMatch.profile_version == profile_version)
        .order_by(JobMatch.llm_score.desc(), JobMatch.job_id)
    )
    return list(rows.all())
