"""Task kind -> handler for the poll-loop worker (jobfinder.worker, TASKS_BACKEND=legacy).

The handlers themselves live in tasks.ai and tasks.io (#48), registered with @ledger_task as
`(db, ai, task, payload)`. This module adapts each to the legacy `(db, ai, task)` signature, validating
the payload with the same model the ledger runtime uses, so both backends run the same code. It goes
away with the legacy worker. Handlers return a JSON-serialisable result stored on the task.
"""
from collections.abc import Awaitable, Callable
from functools import wraps

from sqlalchemy.ext.asyncio import AsyncSession

import jobfinder.tasks.ai  # importing the two handler modules registers their handlers
import jobfinder.tasks.io  # noqa: F401
from jobfinder.integrations.claude.tasks import AITasks
from jobfinder.models import Task
from jobfinder.tasks.ai import MAX_LINK_CHARS, REINDUCE_RETRIES  # noqa: F401  (re-exported)
from jobfinder.tasks.runtime import PAYLOADS, REGISTRY, parse_payload

Handler = Callable[[AsyncSession, AITasks, Task], Awaitable[dict]]


def _legacy(kind: str) -> Handler:
    """The @ledger_task handler for `kind` as a legacy handler: same payload model, same code."""
    handler, payload = REGISTRY[kind], PAYLOADS[kind]

    @wraps(handler)
    async def run(db: AsyncSession, ai: AITasks, task: Task) -> dict:
        return await handler(db, ai, task, parse_payload(payload, task.payload))

    return run


extract_profile = _legacy("extract_profile")
followup_questions = _legacy("followup_questions")
audit_profile = _legacy("audit_profile")
match_user = _legacy("match_user")
collect_questions = _legacy("collect_questions")
daily_emails = _legacy("daily_emails")
cover_letter = _legacy("cover_letter")
resume_tailor = _legacy("resume_tailor")
ai_check = _legacy("ai_check")
resolve_job_link = _legacy("resolve_job_link")
audit_coverage = _legacy("audit_coverage")
induce_recipe = _legacy("induce_recipe")
reinduce_recipe = _legacy("reinduce_recipe")
ingest_jobs = _legacy("ingest_jobs")
ingest_tick = _legacy("ingest_tick")
ingest_board = _legacy("ingest_board")
ingest_source = _legacy("ingest_source")
plan_work = _legacy("plan_work")
discover_boards = _legacy("discover_boards")
score_coveted = _legacy("score_coveted")

HANDLERS: dict[str, Handler] = {
    "extract_profile": extract_profile,
    "followup_questions": followup_questions,
    "audit_profile": audit_profile,
    "ingest_jobs": ingest_jobs,
    "ingest_tick": ingest_tick,
    "ingest_board": ingest_board,
    "ingest_source": ingest_source,
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
    "induce_recipe": induce_recipe,
    "reinduce_recipe": reinduce_recipe,
}
