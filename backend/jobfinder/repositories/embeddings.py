"""The embedding index (#126): which postings and profile versions still need vectors, and writing them.

`SOURCE_HASH` is the one definition of "what a posting's vector was built from": md5 of its title,
company and description. It is computed in SQL both to find stale postings and to store with each
vector, so the two can never disagree. Nothing here commits; services.matching.index's callers do.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import Job, JobEmbedding, Profile, ProfileEmbedding, ProfileVersion

SOURCE_HASH = sa.func.md5(
    sa.func.coalesce(Job.title, "") + "|" + sa.func.coalesce(Job.company_name, "") + "|"
    + sa.func.coalesce(Job.description_text, ""),
    type_=sa.String,
)


def _stale(model: str):
    """Active postings with no vector from `model`, or one built from text that has since changed."""
    current = sa.exists().where(JobEmbedding.job_id == Job.id, JobEmbedding.model == model,
                                JobEmbedding.source_hash == SOURCE_HASH)
    return sa.and_(Job.is_active.is_(True), ~current)


async def stale_jobs(db: AsyncSession, model: str, limit: int) -> list[dict]:
    """Up to `limit` postings to embed, newest first (new postings before the backfill): each a dict
    with id, title, company_name, description_text and the source_hash of exactly that text."""
    rows = await db.execute(
        sa.select(Job.id, Job.title, Job.company_name, Job.description_text, SOURCE_HASH.label("source_hash"))
        .where(_stale(model)).order_by(Job.first_seen_at.desc(), Job.id.desc()).limit(limit)
    )
    return [dict(r._mapping) for r in rows]


async def any_stale_jobs(db: AsyncSession, model: str) -> bool:
    return bool(await db.scalar(sa.select(sa.exists().where(_stale(model)))))


async def upsert_job_embeddings(db: AsyncSession, model: str,
                                rows: Sequence[tuple[int, str, list[float]]]) -> None:
    """Store (job_id, source_hash, vector) rows, replacing the posting's earlier vector from `model`."""
    if not rows:
        return
    stmt = pg_insert(JobEmbedding).values(
        [{"job_id": job_id, "model": model, "source_hash": h, "embedding": v} for job_id, h, v in rows]
    )
    await db.execute(stmt.on_conflict_do_update(
        index_elements=[JobEmbedding.job_id, JobEmbedding.model],
        set_={"source_hash": stmt.excluded.source_hash, "embedding": stmt.excluded.embedding,
              "embedded_at": sa.func.now()},
    ))


async def delete_inactive_job_embeddings(db: AsyncSession, model: str) -> int:
    """Drop `model`'s vectors of postings that are no longer active. Returns the rows deleted."""
    inactive = sa.select(Job.id).where(Job.is_active.is_(False))
    result = await db.execute(sa.delete(JobEmbedding).where(JobEmbedding.model == model,
                                                            JobEmbedding.job_id.in_(inactive)))
    return result.rowcount or 0


def _has_vectors(model: str):
    return sa.exists().where(ProfileEmbedding.user_id == Profile.user_id, ProfileEmbedding.model == model,
                             ProfileEmbedding.profile_version == Profile.version)


async def profiles_without_vectors(db: AsyncSession, model: str) -> list[tuple[int, dict, dict]]:
    """(user_id, status, background) of every profile whose current version has no vectors from `model`."""
    rows = await db.execute(
        sa.select(Profile.user_id, Profile.status, Profile.background).where(~_has_vectors(model))
        .order_by(Profile.user_id)
    )
    return [(uid, status or {}, background or {}) for uid, status, background in rows]


async def has_profile_vectors(db: AsyncSession, user_id: int, model: str, version: int) -> bool:
    return bool(await db.scalar(sa.select(sa.exists().where(
        ProfileEmbedding.user_id == user_id, ProfileEmbedding.model == model,
        ProfileEmbedding.profile_version == version))))


async def insert_profile_embeddings(db: AsyncSession, user_id: int, model: str, version: int,
                                    rows: Sequence[tuple[int, str, list[float]]]) -> None:
    """Store a profile version's (ordinal, text, vector) query rows; rows already there are kept."""
    if not rows:
        return
    await db.execute(pg_insert(ProfileEmbedding).values([
        {"user_id": user_id, "model": model, "profile_version": version, "ordinal": o, "text": t, "embedding": v}
        for o, t, v in rows
    ]).on_conflict_do_nothing())


async def delete_older_profile_embeddings(db: AsyncSession, user_id: int, model: str, version: int) -> int:
    """Drop the user's vectors from `model` for versions before `version`. Returns the rows deleted."""
    result = await db.execute(sa.delete(ProfileEmbedding).where(
        ProfileEmbedding.user_id == user_id, ProfileEmbedding.model == model,
        ProfileEmbedding.profile_version < version))
    return result.rowcount or 0


async def save_profile_version(db: AsyncSession, user_id: int, version: int, status: dict, background: dict,
                               facts: list[dict]) -> None:
    """Snapshot what the profile says at `version`; the first snapshot of a version is the one kept."""
    await db.execute(pg_insert(ProfileVersion).values(
        user_id=user_id, version=version, status=status, background=background, facts=facts,
    ).on_conflict_do_nothing())


async def delete_model(db: AsyncSession, model: str) -> tuple[int, int]:
    """Delete every posting and profile vector from `model`. Returns (job rows, profile rows) deleted."""
    jobs = await db.execute(sa.delete(JobEmbedding).where(JobEmbedding.model == model))
    profiles = await db.execute(sa.delete(ProfileEmbedding).where(ProfileEmbedding.model == model))
    return jobs.rowcount or 0, profiles.rowcount or 0
