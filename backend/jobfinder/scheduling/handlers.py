"""Task kind -> handler. Handlers return a JSON-serialisable result stored on the task."""
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.ai.claude_code import AIError
from jobfinder.ai.tasks import AITasks
from jobfinder.models import Profile, ProfileFact, Task
from jobfinder.scheduling import queue
from jobfinder.storage.documents import get_store, is_tailored

Handler = Callable[[AsyncSession, AITasks, Task], Awaitable[dict]]

log = logging.getLogger("jobfinder.handlers")
MAX_LINK_CHARS = 8_000
REINDUCE_RETRIES = 6  # re-induction attempts put off by a rate limit or an unreachable site


async def extract_profile(db: AsyncSession, ai: AITasks, task: Task) -> dict:
    store = get_store()
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


async def followup_questions(db: AsyncSession, ai: AITasks, task: Task) -> dict:
    uid = task.user_id
    profile = await db.get(Profile, uid)
    facts = (await db.execute(select(ProfileFact).where(ProfileFact.user_id == uid).order_by(ProfileFact.id))).scalars()
    qs = await ai.followup_questions(
        profile.status if profile else {},
        profile.background if profile else {},
        [(f.question, f.answer) for f in facts],
    )
    return {"questions": [q.model_dump() for q in qs]}




async def audit_profile(db: AsyncSession, ai: AITasks, task: Task) -> dict:
    from jobfinder.profile import interview

    return await interview.run_audit(db, ai, task.user_id)


async def ingest_jobs(db: AsyncSession, ai: AITasks, task: Task) -> dict:
    """Poll all ATS boards (and jobspy/usajobs when enabled). Scheduling this is milestone 7."""
    from jobfinder.config import get_settings
    from jobfinder.db import SessionLocal
    from jobfinder.ingest.runner import run_ingest

    summary = await run_ingest(SessionLocal)
    # Matching is planned, not fanned out: the planner picks who needs it most and staggers the runs.
    # Adaptive ticks come every few minutes and often fetch nothing: plan only when jobs changed.
    if not get_settings().adaptive_schedule_enabled or summary.get("new") or summary.get("deactivated"):
        await queue.enqueue(db, "plan_work", dedupe=True)
    return summary


async def plan_work(db: AsyncSession, ai: AITasks, task: Task) -> dict:
    from jobfinder.scheduling import planner

    return await planner.plan(db)


async def match_user(db: AsyncSession, ai: AITasks, task: Task) -> dict:
    from jobfinder.matching import service

    summary = await service.match_user(db, ai, task.user_id)
    if summary.get("scored"):
        # New assessments may have raised questions worth asking.
        await queue.enqueue(db, "collect_questions", user_id=task.user_id, dedupe=True)
    return summary


async def collect_questions(db: AsyncSession, ai: AITasks, task: Task) -> dict:
    from jobfinder.questions import service

    return await service.collect_questions(db, ai, task.user_id)


async def daily_emails(db: AsyncSession, ai: AITasks, task: Task) -> dict:
    """Hourly tick: send the daily email to every user whose chosen hour has come."""
    from datetime import UTC, datetime

    from jobfinder.notify import daily
    from jobfinder.notify.email import EmailError
    from jobfinder.questions import service as questions

    now = datetime.now(UTC)
    out = {"due": 0, "sent": 0, "nothing_to_send": 0, "errors": {}}
    for user in await daily.due_users(db, now):
        out["due"] += 1
        try:
            if user.question_emails_enabled:
                try:
                    await questions.collect_questions(db, ai, user.id)
                except AIError as e:  # still send the matches; questions go out tomorrow
                    log.warning("question collection for user %s failed: %s", user.id, e)
            result = await daily.send_daily(db, user, now=now)
            out["sent" if result else "nothing_to_send"] += 1
        except EmailError as e:
            log.warning("daily email for user %s failed: %s", user.id, e)
            out["errors"][str(user.id)] = str(e)[:200]
        except Exception as e:
            log.exception("daily email for user %s crashed", user.id)
            out["errors"][str(user.id)] = f"{type(e).__name__}: {e}"[:200]
            await db.rollback()
    return out


async def cover_letter(db: AsyncSession, ai: AITasks, task: Task) -> dict:
    from jobfinder.letters import service

    p = task.payload
    return await service.generate(db, ai, task.user_id, p["match_id"], p.get("tone", "professional"), p.get("notes", ""))


async def resume_tailor(db: AsyncSession, ai: AITasks, task: Task) -> dict:
    from jobfinder.resumes import service

    return await service.tailor(db, ai, get_store(), task.user_id, task.payload["match_id"], force=bool(task.payload.get("force")))


async def ai_check(db: AsyncSession, ai: AITasks, task: Task) -> dict:
    """Report whether Claude is connected. Failures are returned, not raised, so the admin page
    shows the real reason and nothing is retried."""
    try:
        await ai.ping()
    except AIError as e:
        return {"ok": False, "error_kind": e.kind, "error": e.message}
    return {"ok": True}


async def discover_boards(db: AsyncSession, ai: AITasks, task: Task) -> dict:
    from jobfinder.ingest import discovery

    return (await discovery.discover_boards(db)).as_dict()


async def resolve_job_link(db: AsyncSession, ai: AITasks, task: Task) -> dict:
    """A pasted job link whose board is new: add it, fetch it now, then let the planner match the new jobs."""
    from jobfinder.ingest.resolve import resolve_link

    out = await resolve_link(db, int(task.payload["link_id"]), allow_llm=await _llm_allowed(db), ai=ai)
    if out.get("new"):
        await queue.enqueue(db, "plan_work", dedupe=True)
    return out


async def score_coveted(db: AsyncSession, ai: AITasks, task: Task) -> dict:
    """Weekly: recompute how sought-after each employer is, then audit coverage of the top tiers."""
    from jobfinder.ingest import coveted

    out = await coveted.score_all(db)
    if not task.payload.get("no_audit"):
        await queue.enqueue(db, "audit_coverage", dedupe=True)
    return out


async def audit_coverage(db: AsyncSession, ai: AITasks, task: Task) -> dict:
    """Send coveted employers with no working board through the resolver (payload may name just one)."""
    from jobfinder.ingest import coveted

    p = task.payload
    out = await coveted.audit_coverage(
        db, company_id=int(p["company_id"]) if p.get("company_id") is not None else None,
        target_id=int(p["target_id"]) if p.get("target_id") is not None else None,
    )
    if out.get("resolved"):
        await queue.enqueue(db, "plan_work", dedupe=True)
    return out


async def _llm_allowed(db: AsyncSession) -> bool:
    """The resolver may ask Claude unless it's switched off or the planner is backing off a rate limit."""
    from datetime import UTC, datetime

    from jobfinder.config import get_settings
    from jobfinder.scheduling import planner

    return get_settings().resolver_llm_enabled and await planner.backoff_until(db, datetime.now(UTC)) is None


async def reinduce_recipe(db: AsyncSession, ai: AITasks, task: Task) -> dict:
    """Self-heal a degraded recipe board (queued by ingest_company): ask Claude for a new recipe, at most
    twice a week per domain (ingest.recipes.health)."""
    from datetime import UTC, datetime, timedelta

    from jobfinder.config import get_settings
    from jobfinder.ingest.resolve.rungs.llm import reinduce_company

    company_id, attempt = int(task.payload["company_id"]), int(task.payload.get("attempt", 0))
    if not get_settings().resolver_llm_enabled:
        return {"skipped": "llm disabled"}
    if await _llm_allowed(db):
        out = await reinduce_company(db, ai, company_id)
    else:
        out = {"skipped": "AI backing off", "retry": True}
    # Not now (rate limit, AI down, site unreachable): an auto-disabled board is never fetched again, so no
    # later run would queue it. Try again after the planner's backoff, a few times.
    if out.pop("retry", False) and attempt < REINDUCE_RETRIES:
        later = datetime.now(UTC) + timedelta(minutes=get_settings().planner_backoff_minutes)
        await queue.enqueue(db, "reinduce_recipe", {"company_id": company_id, "attempt": attempt + 1},
                            run_after=later, dedupe=True)
        out["requeued"] = later.isoformat()
    return out


HANDLERS: dict[str, Handler] = {
    "extract_profile": extract_profile,
    "followup_questions": followup_questions,
    "audit_profile": audit_profile,
    "ingest_jobs": ingest_jobs,
    "plan_work": plan_work,
    "ai_check": ai_check,
    "match_user": match_user,
    "cover_letter": cover_letter,
    "collect_questions": collect_questions,
    "daily_emails": daily_emails,
    "resume_tailor": resume_tailor,
    "discover_boards": discover_boards,
    "resolve_job_link": resolve_job_link,
    "score_coveted": score_coveted,
    "audit_coverage": audit_coverage,
    "reinduce_recipe": reinduce_recipe,
}
