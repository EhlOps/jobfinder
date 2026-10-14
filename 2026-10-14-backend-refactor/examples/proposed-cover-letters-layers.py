# Sketch (v2): one change, "draft a cover letter", across the layers by kind (D1, D2, D3, D6).
# Same endpoint, same response, same task kind string. Each section is its own file.

# ---------------------------------------------------------------- api/routers/cover_letters.py
from fastapi import APIRouter

from jobfinder.api.deps import CurrentUser, DbSession
from jobfinder.schemas.common import TaskRef
from jobfinder.schemas.cover_letters import GenerateIn
from jobfinder.services import cover_letters

router = APIRouter(prefix="/api/matches/{match_id}/cover-letter", tags=["cover letters"])


@router.post("", response_model=TaskRef, status_code=202)
async def generate_letter(match_id: int, body: GenerateIn, user: CurrentUser, db: DbSession):
    """Draft (or redraft) the letter in worker-ai. Poll GET /api/tasks/{id}, then GET this resource."""
    task = await cover_letters.request_draft(db, user, match_id, body)
    return TaskRef(task_id=task.id)


# ---------------------------------------------------------------- schemas/cover_letters.py
from pydantic import BaseModel, Field


class GenerateIn(BaseModel):
    tone: Tone = "professional"
    notes: str = Field("", max_length=2000)
    force: bool = False


# ---------------------------------------------------------------- services/cover_letters.py  (rules, no SQL)
from jobfinder.core.errors import Conflict, Invalid
from jobfinder.repositories import letters, matches, profiles
from jobfinder.tasks import ledger


class ProfileIncomplete(Invalid):
    code = "profile_incomplete"
    message = "Finish your profile first (onboarding)"


class LetterEdited(Conflict):
    code = "letter_edited"
    message = "You've edited this letter; redrafting would replace your changes."


async def request_draft(db, user, match_id: int, body: GenerateIn):
    match, job = await matches.get_owned(db, user.id, match_id)       # raises NotFound
    profile = await profiles.get_or_default(db, user.id)
    if not profile.background:
        raise ProfileIncomplete()
    existing = await letters.current(db, user.id, job.id)
    if existing and existing.edited and not body.force:
        raise LetterEdited()
    # A ledger row, flushed only. It commits with the request; the api publishes it after COMMIT.
    return await ledger.add(db, "cover_letter", queue="ai", user_id=user.id, dedupe=True,
                            payload={"match_id": match_id, "tone": body.tone, "notes": body.notes.strip()})


async def generate(ctx, user_id: int, match_id: int, tone: str, notes: str) -> dict:
    match, job = await matches.get_owned(ctx.db, user_id, match_id)
    profile = await profiles.get_or_default(ctx.db, user_id)
    if not profile.background:
        raise ProfileIncomplete()            # same rule, one place; the worker marks the task failed
    content = await ctx.claude.cover_letter(job=job, profile=profile, tone=tone, notes=notes)
    await letters.upsert(ctx.db, user_id, job.id, content, profile.version)    # no commit
    return {"words": len(content.split())}


# ---------------------------------------------------------------- repositories/letters.py  (SQL only)
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from jobfinder.models.cover_letter import CoverLetter


async def current(db, user_id: int, job_id: int) -> CoverLetter | None:
    return await db.scalar(sa.select(CoverLetter).where(CoverLetter.user_id == user_id, CoverLetter.job_id == job_id))


async def upsert(db, user_id: int, job_id: int, content: str, profile_version: int) -> None:
    await db.execute(insert(CoverLetter).values(...).on_conflict_do_update(...))


# ---------------------------------------------------------------- tasks/ai.py  (queue "ai", run by worker-ai)
from jobfinder.tasks.brokers import ai_broker
from jobfinder.tasks.runtime import TaskPayload, ledger_task


class Draft(TaskPayload):
    match_id: int
    tone: Tone = "professional"
    notes: str = ""


@ledger_task(ai_broker, "cover_letter", payload=Draft)   # kind unchanged: rows queued before the deploy still run
async def draft_letter(ctx, p: Draft) -> dict:
    return await cover_letters.generate(ctx, ctx.task.user_id, p.match_id, p.tone, p.notes)
