"""Clarifying questions: collect the 'unknowns' from uncertain matches, merge duplicates, ask the
candidate (email or in-app), turn answers into profile facts, and queue re-scoring. Also the signed email
links (questions and unsubscribe). Nothing here commits: the request, task or CLI boundary does."""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from jobfinder.core.config import settings
from jobfinder.core.db import AsyncSession
from jobfinder.core.errors import BadRequest, Gone
from jobfinder.integrations.claude.tasks import AITasks
from jobfinder.models import ClarifyingQuestion, ProfileFact
from jobfinder.notify.tokens import InvalidToken, read_token
from jobfinder.repositories import profiles
from jobfinder.repositories import questions as repo
from jobfinder.schemas.questions import MAX_ANSWER_CHARS
from jobfinder.services import profile as profile_service
from jobfinder.tasks import ledger

log = logging.getLogger("jobfinder.questions")
MAX_RAW = 60  # matches reviewed per run


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", s.lower()).strip()


class LinkExpired(Gone):
    code = "link_expired"


class LinkInvalid(BadRequest):
    code = "link_invalid"


def user_from_token(purpose: str, token: str) -> int:
    """The user a signed email link was made for, or LinkExpired / LinkInvalid."""
    try:
        return read_token(purpose, token)
    except InvalidToken as e:
        if e.expired:
            raise LinkExpired("This link has expired. Log in and answer from the Questions page instead.") from None
        raise LinkInvalid("This link isn't valid.") from None


async def unsubscribe(db: AsyncSession, user_id: int) -> None:
    """Turn off both emails (the daily digest and question emails). A deleted user is a no-op."""
    user = await repo.user(db, user_id)
    if user:
        user.digest_enabled = False
        user.question_emails_enabled = False


async def open_count(db: AsyncSession, user_id: int) -> int:
    return await repo.open_count(db, user_id)


async def collect_questions(db: AsyncSession, ai: AITasks, user_id: int) -> dict:
    """Look at matches whose unknowns haven't been reviewed yet and turn them into a short list of
    deduplicated questions, most-asked first. Every score band counts: a low score is often just a gap
    the candidate can fill. No LLM call when there is nothing to review."""
    if await open_count(db, user_id) >= settings.max_open_questions:
        return {"new_questions": 0, "reviewed": 0, "skipped": "too many unanswered questions already"}

    rows = await repo.unreviewed_matches(db, user_id, MAX_RAW)
    if not rows:
        return {"new_questions": 0, "reviewed": 0}

    # Rank by how many jobs raise the same question (a must-have's question comes first within a job).
    tally: dict[str, list] = {}
    for m in rows:
        for rank, u in enumerate(m.unknowns or []):
            q = (u.get("question") or "").strip()
            if q:
                t = tally.setdefault(_norm(q), [0, rank, q, (u.get("why") or "").strip(), []])
                t[0] += 1
                t[4].append(m.id)
                t[1] = min(t[1], rank)
    ranked = sorted(tally.values(), key=lambda t: (-t[0], t[1]))
    raw = [(t[2], t[3]) for t in ranked]
    raw_matches = [t[4] for t in ranked]  # per question: the matches that raised it
    reviewed_ids = [m.id for m in rows]

    created = 0
    if raw:
        facts = [(f.question, f.answer) for f in await profiles.facts(db, user_id)]
        asked = await repo.asked(db, user_id)
        room = max(0, settings.max_open_questions - await open_count(db, user_id))
        result = await ai.consolidate_questions(facts, asked, raw, max_questions=min(room, settings.questions_per_email))
        existing = {_norm(q) for q, _ in asked} | {_norm(q) for q, _ in facts}
        for cq in result.questions[:room]:
            text = cq.question.strip()
            if not text or _norm(text) in existing:
                continue
            ids = sorted({mid for i in cq.sources if 0 <= i < len(raw_matches) for mid in raw_matches[i]} or set(raw_matches[0]))
            repo.add(db, ClarifyingQuestion(user_id=user_id, question=text[:500], why=cq.why.strip()[:300], match_ids=ids))
            existing.add(_norm(text))
            created += 1

    # Only mark reviewed once the consolidation succeeded (an AI error above leaves them for next time).
    await repo.mark_reviewed(db, reviewed_ids)
    return {"new_questions": created, "reviewed": len(reviewed_ids)}


async def list_open(db: AsyncSession, user_id: int) -> list[dict]:
    qs = await repo.open_for(db, user_id)
    jobs = await repo.match_jobs(db, user_id, {mid for q in qs for mid in q.match_ids or []})
    return [
        {"id": q.id, "question": q.question, "why": q.why, "jobs": [jobs[m] for m in (q.match_ids or []) if m in jobs][:3]}
        for q in qs
    ]


async def answer_questions(db: AsyncSession, user_id: int, answers: list[dict]) -> dict:
    """answers: [{id, answer?, skip?}]. Answers become profile facts; the profile version bump makes the
    affected matches stale so the next matching run re-scores them. Unknown or foreign ids are ignored."""
    ids = [a["id"] for a in answers]
    owned = await repo.open_by_ids(db, user_id, ids)
    answered = skipped = 0
    facts: list[ProfileFact] = []
    now = datetime.now(UTC)
    for a in answers:
        q = owned.get(a["id"])
        if not q:
            continue
        text = (a.get("answer") or "").strip()[:MAX_ANSWER_CHARS]
        if text:
            q.status, q.answer, q.answered_at = "answered", text, now
            facts.append(ProfileFact(user_id=user_id, question=q.question, answer=text, source="job_question"))
            answered += 1
        elif a.get("skip"):
            q.status, q.answered_at = "skipped", now
            skipped += 1
    profiles.add_facts(db, facts)
    if answered:
        profile = await profiles.get(db, user_id)
        if profile:
            profile_service.bump_version(profile)
        await ledger.add(db, "match_user", user_id=user_id, dedupe=True)
    return {"answered": answered, "skipped": skipped, "remaining": await open_count(db, user_id)}
