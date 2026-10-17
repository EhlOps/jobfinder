"""The `ai` queue's handlers: every kind that may call Claude (tasks.kinds). worker-ai imports this
module (`python -m jobfinder.tasks ai`); it is the only worker image with the Claude CLI.

Each handler is `(db, ai, task, payload) -> dict`, registered with @ledger_task: the runtime commits its
result (and any follow-up `ledger.add`, which is published once that commit lands). The commits a
handler makes itself are its own boundaries, kept from the poll-loop worker. scheduling.handlers adapts
these same functions for that worker while TASKS_BACKEND=legacy.
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


class ResolveLinkArgs(BaseModel):
    link_id: int


class AuditCoverageArgs(BaseModel):
    company_id: int | None = None  # audit just this employer's targets
    target_id: int | None = None  # or just this one


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
    from jobfinder.matching import service

    # Commit after each scored job: a rate limit mid-run keeps what was scored (the runtime rolls back the rest).
    summary = await service.match_user(db, ai, task.user_id, checkpoint=db.commit)
    if summary.get("scored"):
        # New assessments may have raised questions worth asking.
        await ledger.add(db, "collect_questions", user_id=task.user_id, dedupe=True)
    return summary


@ledger_task(ai_broker, "collect_questions")
async def collect_questions(db: AsyncSession, ai: AITasks, task: Task, _: None) -> dict:
    """The runtime's completion commit is the boundary: the new questions and the reviewed flags go together."""
    from jobfinder.services import questions as service

    return await service.collect_questions(db, ai, task.user_id)


@ledger_task(ai_broker, "daily_emails")
async def daily_emails(db: AsyncSession, ai: AITasks, task: Task, _: None) -> dict:
    """Hourly tick: send the daily email to every user whose chosen hour has come (services.digest)."""
    from jobfinder.services import digest

    return await digest.send_due(db, ai, datetime.now(UTC))


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


# ---------------------------------------------------------------- resolver (may ask Claude)


async def _llm_allowed(db: AsyncSession) -> bool:
    """The resolver may ask Claude unless it's switched off or the planner is backing off a rate limit."""
    from jobfinder.core.config import settings
    from jobfinder.scheduling import planner

    return settings.resolver_llm_enabled and await planner.backoff_until(db, datetime.now(UTC)) is None


@ledger_task(ai_broker, "resolve_job_link", payload=ResolveLinkArgs)
async def resolve_job_link(db: AsyncSession, ai: AITasks, task: Task, p: ResolveLinkArgs) -> dict:
    """A pasted job link whose board is new: add it, fetch it now, then let the planner match the new jobs."""
    from jobfinder.services.resolver import resolve_link

    out = await resolve_link(db, p.link_id, allow_llm=await _llm_allowed(db), ai=ai)
    await db.commit()  # the boundary: the link's outcome, and the board and jobs it found
    if out.get("new"):
        await ledger.add(db, "plan_work", dedupe=True)
    return out


@ledger_task(ai_broker, "audit_coverage", payload=AuditCoverageArgs)
async def audit_coverage(db: AsyncSession, ai: AITasks, task: Task, p: AuditCoverageArgs) -> dict:
    """Send coveted employers with no working board through the resolver (payload may name just one)."""
    from jobfinder.services import coverage

    out = await coverage.audit_coverage(
        db, company_id=p.company_id, target_id=p.target_id,
        checkpoint=db.commit,  # keep each target's outcome as the audit goes
    )
    await db.commit()
    if out.get("resolved"):
        await ledger.add(db, "plan_work", dedupe=True)
    return out


@ledger_task(ai_broker, "reinduce_recipe", payload=ReinduceArgs)
async def reinduce_recipe(db: AsyncSession, ai: AITasks, task: Task, p: ReinduceArgs) -> dict:
    """Self-heal a degraded recipe board (queued by ingest_company): ask Claude for a new recipe, at most
    twice a week per domain (ingest.recipes.health)."""
    from jobfinder.core.config import settings
    from jobfinder.services.resolver.rungs.llm import reinduce_company

    if not settings.resolver_llm_enabled:
        return {"skipped": "llm disabled"}
    if await _llm_allowed(db):
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
