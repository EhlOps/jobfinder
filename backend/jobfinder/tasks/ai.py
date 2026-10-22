"""The `ai` queue's handlers: every kind that may call Claude (tasks.kinds). worker-ai imports this
module (`python -m jobfinder.tasks ai`); it is the only worker image with the Claude CLI.

Each handler is `(db, ai, task, payload) -> dict`, registered with @ledger_task: the runtime commits its
result (and any follow-up `ledger.add`, which is published once that commit lands). The commits a
handler makes itself are its own boundaries.
"""
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.context import default_context
from jobfinder.integrations.claude.claude_code import AIError
from jobfinder.integrations.claude.tasks import AITasks
from jobfinder.models import Profile, ProfileFact, Task
from jobfinder.storage.documents import is_tailored
from jobfinder.tasks import ledger
from jobfinder.tasks.brokers import ai_broker
from jobfinder.tasks.runtime import ledger_task

MAX_LINK_CHARS = 8_000
REINDUCE_RETRIES = 6  # re-induction attempts put off by a rate limit or an unreachable site
INDUCE_RETRIES = 3  # a pasted link's induction put off by a rate limit, before it's left unresolved


# ---------------------------------------------------------------- payloads
# Lenient on purpose: rows queued before a deploy must still validate (unknown keys are ignored, the
# defaults are the ones the handlers always used).


class CoverLetterArgs(BaseModel):
    match_id: int
    tone: str = "professional"
    notes: str = ""


class ResumeTailorArgs(BaseModel):
    match_id: int
    force: bool = False


class InduceRecipeArgs(BaseModel):
    """What resolve_job_link's deterministic rungs found (services.resolver._hints)."""
    link_id: int
    samples: list[tuple[str, str]] = []  # same-site JSON (url, body) they fetched
    tried: list[str] = []  # URLs they tried as JSON
    notes: list[str] = []
    single_job: dict | None = None  # the posting alone, kept if no board turns up
    board_error: str | None = None
    attempt: int = 0


class ReinduceArgs(BaseModel):
    company_id: int
    attempt: int = 0


# ---------------------------------------------------------------- profile


@ledger_task(ai_broker, "extract_profile")
async def extract_profile(db: AsyncSession, ai: AITasks, task: Task, _: None) -> dict:
    store = default_context().store
    sources = []
    for doc in store.list(task.user_id):
        if is_tailored(doc):  # AI-tailored output must not become ground truth
            continue
        text = store.read_text(task.user_id, doc.id)
        if not text:
            continue
        if doc.kind == "link":
            label, text = f"{doc.tags.get('link_kind', 'link')}: {doc.tags.get('url', '')}", text[:MAX_LINK_CHARS]
        else:
            label = doc.filename
        sources.append((label, text))
    if not sources:
        raise ValueError("No readable documents or links to extract from")
    return (await ai.extract_profile(sources)).model_dump()


@ledger_task(ai_broker, "followup_questions")
async def followup_questions(db: AsyncSession, ai: AITasks, task: Task, _: None) -> dict:
    uid = task.user_id
    profile = await db.get(Profile, uid)
    facts = (await db.execute(select(ProfileFact).where(ProfileFact.user_id == uid).order_by(ProfileFact.id))).scalars()
    qs = await ai.followup_questions(
        profile.status if profile else {},
        profile.background if profile else {},
        [(f.question, f.answer) for f in facts],
    )
    return {"questions": [q.model_dump() for q in qs]}


@ledger_task(ai_broker, "audit_profile")
async def audit_profile(db: AsyncSession, ai: AITasks, task: Task, _: None) -> dict:
    """The runtime's completion commit is the boundary; run_audit explains when its row lock ends."""
    from jobfinder.services import interview

    return await interview.run_audit(db, ai, task.user_id)


# ---------------------------------------------------------------- matching and questions


@ledger_task(ai_broker, "match_user")
async def match_user(db: AsyncSession, ai: AITasks, task: Task, _: None) -> dict:
    """One slice of MATCH_SLICE_JOBS scores. While it scored something and work and budget remain, it
    queues the next slice at the back of the ai stream, so other users' slices take turns with it. The
    rerun dedupe folds a profile edit made mid-chain into that one queued slice (services.questions).
    The payload stays empty for that reason: a per-chain counter would give the slices their own dedupe key."""
    from jobfinder.core.config import settings
    from jobfinder.services import questions
    from jobfinder.services.matching import service

    # Commit after each scored job: a rate limit mid-slice keeps what was scored (the runtime rolls back the rest).
    limit = max(1, settings.match_slice_jobs)  # 0 would score nothing, ever
    summary = await service.match_user(db, ai, task.user_id, limit=limit, checkpoint=db.commit)
    if summary.get("scored") and summary.get("pending") and summary.get("budget_left"):
        # The id lets the matches page follow the chain (frontend routes/Matches.tsx).
        summary["next_slice"] = (await ledger.add(db, "match_user", user_id=task.user_id, rerun=True)).id
    elif summary.get("scored") or await questions.has_unreviewed(db, task.user_id):
        # The chain ends here: its new assessments may have raised questions worth asking. The second check
        # covers a chain whose last slice scored nothing, or an earlier chain that stopped on an error.
        await ledger.add(db, "collect_questions", user_id=task.user_id, dedupe=True)
    return summary


@ledger_task(ai_broker, "collect_questions")
async def collect_questions(db: AsyncSession, ai: AITasks, task: Task, _: None) -> dict:
    """The runtime's completion commit is the boundary: the new questions and the reviewed flags go together.
    The payload is empty, or names the digest it runs ahead of (services.digest.schedule_questions)."""
    from jobfinder.services import questions as service

    return await service.collect_questions(db, ai, task.user_id)


# ---------------------------------------------------------------- documents


@ledger_task(ai_broker, "cover_letter", payload=CoverLetterArgs)
async def cover_letter(db: AsyncSession, ai: AITasks, task: Task, p: CoverLetterArgs) -> dict:
    """The draft is written in the task's transaction; the runtime commits it with the task's result."""
    from jobfinder.services import letters as service

    return await service.generate(db, ai, task.user_id, p.match_id, p.tone, p.notes)


@ledger_task(ai_broker, "resume_tailor", payload=ResumeTailorArgs)
async def resume_tailor(db: AsyncSession, ai: AITasks, task: Task, p: ResumeTailorArgs) -> dict:
    from jobfinder.services import resumes as service

    return await service.tailor(db, ai, default_context().store, task.user_id, p.match_id, force=p.force)


@ledger_task(ai_broker, "ai_check")
async def ai_check(db: AsyncSession, ai: AITasks, task: Task, _: None) -> dict:
    """Report whether Claude is connected. Failures are returned, not raised, so the admin page
    shows the real reason and nothing is retried."""
    try:
        await ai.ping()
    except AIError as e:
        return {"ok": False, "error_kind": e.kind, "error": e.message}
    return {"ok": True}


# ---------------------------------------------------------------- resolver's LLM rung


@ledger_task(ai_broker, "induce_recipe", payload=InduceRecipeArgs)
async def induce_recipe(db: AsyncSession, ai: AITasks, task: Task, p: InduceRecipeArgs) -> dict:
    """A pasted link no deterministic rung could read (queued by resolve_job_link on io, #50): ask Claude for
    a recipe, then finish the link, fetch a new board now and let the planner match its jobs."""
    from jobfinder.core.config import settings
    from jobfinder.services.resolver import induce_link, llm_allowed

    allowed, retry = await llm_allowed(db), p.attempt < INDUCE_RETRIES
    if not allowed and retry and settings.resolver_llm_enabled:
        out = {"skipped": "AI backing off", "retry": True}  # the link waited for this: don't give up on it yet
    else:
        out = await induce_link(db, p.link_id, ai, allow_llm=allowed, retry=retry, samples=p.samples,
                                tried=p.tried, notes=p.notes, single_job=p.single_job, board_error=p.board_error)
    await db.commit()  # the boundary: the link's outcome, and the board and jobs it found
    if out.pop("retry", False):  # still 'resolving': try again after the planner's backoff
        later = datetime.now(UTC) + timedelta(minutes=settings.planner_backoff_minutes)
        await ledger.add(db, "induce_recipe", {**p.model_dump(), "attempt": p.attempt + 1},
                         user_id=task.user_id, run_after=later, dedupe=True)
        out["requeued"] = later.isoformat()
    if out.get("new"):
        await ledger.add(db, "plan_work", rerun=True)  # a running plan missed these jobs
    return out


@ledger_task(ai_broker, "reinduce_recipe", payload=ReinduceArgs)
async def reinduce_recipe(db: AsyncSession, ai: AITasks, task: Task, p: ReinduceArgs) -> dict:
    """Self-heal a degraded recipe board (queued by ingest_company): ask Claude for a new recipe, at most
    twice a week per domain (integrations.recipes.health)."""
    from jobfinder.core.config import settings
    from jobfinder.services.resolver import llm_allowed
    from jobfinder.services.resolver.rungs.llm import reinduce_company

    if not settings.resolver_llm_enabled:
        return {"skipped": "llm disabled"}
    if await llm_allowed(db):
        out = await reinduce_company(db, ai, p.company_id)
        await db.commit()  # the boundary: the new recipe (or the disabled board) and its jobs
    else:
        out = {"skipped": "AI backing off", "retry": True}
    # Not now (rate limit, AI down, site unreachable): an auto-disabled board is never fetched again, so no
    # later run would queue it. Try again after the planner's backoff, a few times.
    if out.pop("retry", False) and p.attempt < REINDUCE_RETRIES:
        later = datetime.now(UTC) + timedelta(minutes=settings.planner_backoff_minutes)
        await ledger.add(db, "reinduce_recipe", {"company_id": p.company_id, "attempt": p.attempt + 1},
                         run_after=later, dedupe=True)
        out["requeued"] = later.isoformat()
    return out
