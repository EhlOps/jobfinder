"""Generate a cover letter for one of the user's matches and save it as their current draft."""
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.ai.tasks import TONES, AITasks
from jobfinder.matching.service import job_payload
from jobfinder.models import Company, CoverLetter, Job, JobMatch, Profile, ProfileFact

DEFAULT_TONE = "professional"
VALID_TONES = tuple(TONES)


async def generate(db: AsyncSession, ai: AITasks, user_id: int, match_id: int, tone: str, notes: str = "") -> dict:
    row = (
        await db.execute(
            sa.select(JobMatch, Job, Company)
            .join(Job, Job.id == JobMatch.job_id)
            .outerjoin(Company, Company.id == Job.company_id)
            .where(JobMatch.id == match_id, JobMatch.user_id == user_id)
        )
    ).first()
    if not row:
        raise ValueError("Match not found")
    match, job, company = row
    profile = await db.get(Profile, user_id)
    if not profile or not profile.background:
        raise ValueError("Finish your profile before drafting letters")
    facts = [
        (f.question, f.answer)
        for f in await db.scalars(sa.select(ProfileFact).where(ProfileFact.user_id == user_id).order_by(ProfileFact.id))
    ]
    posting = job_payload(job, company)
    posting.pop("salary", None)  # never let pay talk leak into the letter
    content = await ai.cover_letter(
        name=(profile.background or {}).get("name", ""),
        background=profile.background,
        facts=facts,
        job=posting,
        analysis={"strengths": match.reasons, "gaps": match.gaps},
        tone=tone if tone in TONES else DEFAULT_TONE,
        notes=notes,
    )
    now = datetime.now(UTC)
    values = {
        "user_id": user_id, "job_id": job.id, "content": content, "tone": tone, "edited": False,
        "profile_version": profile.version, "generated_at": now, "updated_at": now,
    }
    stmt = insert(CoverLetter).values(**values)
    await db.execute(
        stmt.on_conflict_do_update(
            constraint="uq_letter_user_job", set_={k: stmt.excluded[k] for k in values if k not in ("user_id", "job_id")}
        )
    )
    await db.commit()
    return {"match_id": match_id, "words": len(content.split())}
