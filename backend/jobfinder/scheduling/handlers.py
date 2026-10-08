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
    """Poll all ATS boards (and jobspy when enabled). Scheduling this is milestone 7."""
    from jobfinder.db import SessionLocal
    from jobfinder.ingest.runner import run_ingest

    summary = await run_ingest(SessionLocal)
    # Matching is planned, not fanned out: the planner picks who needs it most and staggers the runs.
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

    return await service.tailor(db, ai, get_store(), task.user_id, task.payload["match_id"])


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
}
