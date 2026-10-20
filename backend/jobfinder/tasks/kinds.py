"""Which stream delivers each task kind. `ai` is the only queue whose worker runs Claude, so every
kind that calls it belongs there and no other kind does (#50)."""
from typing import Literal

Queue = Literal["ai", "io", "render"]

QUEUES: dict[str, Queue] = {
    "extract_profile": "ai",
    "followup_questions": "ai",
    "audit_profile": "ai",
    "match_user": "ai",
    "collect_questions": "ai",
    "cover_letter": "ai",
    "resume_tailor": "ai",
    "ai_check": "ai",
    "daily_emails": "ai",  # collects questions with Claude before sending
    "induce_recipe": "ai",  # the resolver's LLM rung, for a link resolve_job_link couldn't read
    "reinduce_recipe": "ai",
    "ingest_jobs": "io",
    "plan_work": "io",
    "discover_boards": "io",
    "score_coveted": "io",
    "resolve_job_link": "io",  # deterministic rungs only (#50)
    "audit_coverage": "io",  # its resolver runs never ask Claude
    "render_capture": "render",
    "render_list": "render",
}


def queue_for(kind: str) -> Queue:
    try:
        return QUEUES[kind]
    except KeyError:
        raise ValueError(f"unknown task kind {kind!r}: add it to tasks.kinds.QUEUES") from None
