# Sketch: backend/jobfinder/features/cover_letters/ after the refactor (D1, D2, D3, D5).
# Same endpoint, same response, same task kind. Each section below is its own file.

# ---------------------------------------------------------------- errors.py
from jobfinder.core.errors import Conflict, Invalid


class ProfileIncomplete(Invalid):
    code = "profile_incomplete"
    message = "Finish your profile first (onboarding)"


class LetterEdited(Conflict):
    code = "letter_edited"
    message = "You've edited this letter; redrafting would replace your changes."


# ---------------------------------------------------------------- schemas.py
from pydantic import BaseModel, Field

from jobfinder.core.schemas import TaskRef  # noqa: F401  (shared, was copied 7x)


class GenerateIn(BaseModel):
    tone: Tone = "professional"
    notes: str = Field("", max_length=2000)
    force: bool = False


# ---------------------------------------------------------------- service.py
from jobfinder.core.db import Session
from jobfinder.features.matching.service import get_owned_match  # public API of another feature
from jobfinder.features.profile.service import get_profile
from jobfinder.tasks import queue


async def request_draft(db: Session, user: User, match_id: int, body: GenerateIn) -> Task:
    match, job = await get_owned_match(db, user, match_id)  # raises NotFound
    profile = await get_profile(db, user.id)
    if not profile.background:
        raise ProfileIncomplete()
    existing = await current_letter(db, user.id, job.id)
    if existing and existing.edited and not body.force:
        raise LetterEdited()
    # flushes only: the task row commits with the request, or not at all
    return await queue.enqueue(db, Draft(match_id=match_id, tone=body.tone, notes=body.notes.strip()),
                               user_id=user.id, dedupe=True)


async def generate(ctx: TaskContext, user_id: int, p: Draft) -> dict:
    match, job = await get_owned_match(ctx.db, user_id, p.match_id)
    profile = await get_profile(ctx.db, user_id)
    if not profile.background:
        raise ProfileIncomplete()  # same rule, one place; the Runner marks the task failed (no retry)
    content = await ctx.ai.run(CoverLetterPrompt(job=job, profile=profile, tone=p.tone, notes=p.notes))
    await upsert_letter(ctx.db, user_id, job.id, content, profile.version)  # no commit
    return {"words": len(content.split())}


# ---------------------------------------------------------------- router.py
from fastapi import APIRouter

from jobfinder.core.deps import CurrentUser, DbSession

router = APIRouter(prefix="/api/matches/{match_id}/cover-letter", tags=["cover letters"])


@router.post("", response_model=TaskRef, status_code=202)
async def generate_letter(match_id: int, body: GenerateIn, user: CurrentUser, db: DbSession):
    """Draft (or redraft) the letter in the worker. Poll GET /api/tasks/{id}, then GET this resource."""
    task = await service.request_draft(db, user, match_id, body)
    return TaskRef(task_id=task.id)


# ---------------------------------------------------------------- tasks.py
from jobfinder.tasks import TaskContext, TaskPayload, task


class Draft(TaskPayload):
    match_id: int
    tone: Tone = "professional"
    notes: str = ""


@task("cover_letter", payload=Draft)  # kind string unchanged, so queued rows from before the deploy still run
async def draft(ctx: TaskContext, p: Draft) -> dict:
    return await service.generate(ctx, ctx.task.user_id, p)
