"""Tailor the user's resume to one of their matches and keep the result in the file store."""
import json

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.ai.schemas import TailoredResume
from jobfinder.ai.tasks import AITasks
from jobfinder.matching.service import job_payload
from jobfinder.models import Company, Job, JobMatch, Profile, ProfileFact
from jobfinder.storage.documents import DocumentStore

MATCH_TAG = "match"
EDITED_TAG = "edited"
EDITED_MSG = "You edited this resume, so it was not regenerated. Reload and choose overwrite to replace your edits."


def match_tags(match_id: int) -> dict[str, str]:
    return {MATCH_TAG: str(match_id)}


def source_documents(store: DocumentStore, user_id: int) -> list[tuple[str, str]]:
    """Uploads and links the candidate supplied; earlier tailored output is not a source of facts."""
    return [
        (meta.filename, store.read_text(user_id, meta.id))
        for meta in store.list(user_id)
        if MATCH_TAG not in meta.tags and meta.chars > 0
    ]


async def tailor(db: AsyncSession, ai: AITasks, store: DocumentStore, user_id: int, match_id: int, force: bool = False) -> dict:
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
    _, job, company = row
    if not force and _edited(store, user_id, match_id):  # fail before the slow AI call; save() re-checks
        raise ValueError(EDITED_MSG)
    profile = await db.get(Profile, user_id)
    if not profile or not profile.background:
        raise ValueError("Finish your profile before tailoring resumes")
    facts = [
        (f.question, f.answer)
        for f in await db.scalars(sa.select(ProfileFact).where(ProfileFact.user_id == user_id).order_by(ProfileFact.id))
    ]
    posting = job_payload(job, company)
    posting.pop("salary", None)  # pay talk has no place on a resume
    resume = await ai.tailor_resume(
        name=(profile.background or {}).get("name", ""),
        background=profile.background,
        facts=facts,
        documents=source_documents(store, user_id),
        job=posting,
    )
    return save(store, user_id, match_id, resume, f"{job.company_name} {job.title}", force=force)


def _edited(store: DocumentStore, user_id: int, match_id: int) -> bool:
    """Whether the current (newest) resume is user-edited; the same document the API's 409 guard looks at."""
    metas = store.list(user_id, kind="resume", tags=match_tags(match_id))
    return bool(metas) and max(metas, key=lambda m: m.created_at).tags.get(EDITED_TAG) == "1"


def save(
    store: DocumentStore, user_id: int, match_id: int, resume: TailoredResume, label: str = "", force: bool = False,
) -> dict:
    """Store the tailored resume (JSON text) as kind 'resume' tagged match:<id>; one per match, newest wins.

    The user may have edited the resume since this generation was queued (the API only checks at enqueue time),
    so re-check right before writing and refuse to replace edits unless the request was forced."""
    tags = match_tags(match_id)
    if not force and _edited(store, user_id, match_id):
        raise ValueError(EDITED_MSG)
    meta, _ = store.put(
        user_id, "resume", f"Tailored resume - {label or match_id}.json",
        text=json.dumps(resume.model_dump(), indent=1), content_type="application/json", tags=tags,
    )
    for old in store.list(user_id, kind="resume", tags=tags):  # only after the new one is safely written
        if old.id != meta.id and (force or old.tags.get(EDITED_TAG) != "1"):  # an edit landing mid-save survives
            store.delete(user_id, old.id)
    return {"match_id": match_id, "document_id": meta.id}
