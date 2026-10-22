"""Which stream delivers each task kind; this map is the only place a kind's queue is set.

- `priority`: work someone is waiting on (profile work, uploads and links, cover letters, resumes, the
  admin's AI check). worker-priority reads it, so it never queues behind bulk messages on `ai` or `io` (#124).
- `ai`: bulk work that calls Claude (match slices, question collection, recipe induction).
- `io`: everything else that never calls Claude (ingest, planning, email, deterministic link resolving).
- `render`: the renderer's headless-browser captures.

worker-priority and worker-ai are the only workers that run Claude, both through the ClaudeGate
(integrations.claude.gate): it shares the cluster's AI_MAX_CONCURRENCY permits and serves the priority
lane first. So every kind that calls Claude belongs on `priority` or `ai`, and no other kind does (#50)."""
from typing import Literal

Queue = Literal["priority", "ai", "io", "render"]

QUEUES: dict[str, Queue] = {
    "extract_profile": "priority",  # the profile page waits on these three (#124)
    "followup_questions": "priority",
    "audit_profile": "priority",
    "cover_letter": "priority",  # asked for from a match, and awaited there
    "resume_tailor": "priority",
    "ai_check": "priority",  # the admin's "is Claude answering?" ping, which should not wait behind bulk
    "parse_document": "priority",  # uploads and profile links (#51): no Claude, but off io's ingest fan-out
    "fetch_link": "priority",
    "match_user": "ai",  # one slice of MATCH_SLICE_JOBS, which chains the next (#124)
    "collect_questions": "ai",
    "induce_recipe": "ai",  # the resolver's LLM rung, for a link resolve_job_link couldn't read
    "reinduce_recipe": "ai",
    "ingest_tick": "io",  # fans out one ingest_board per due board (#50)
    "ingest_board": "io",
    "ingest_source": "io",  # jobspy / usajobs
    "plan_work": "io",
    "daily_emails": "io",  # sends only: questions are collected on ai ahead of the hour (#50)
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
