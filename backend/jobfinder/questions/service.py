"""Clarifying questions: collect the 'unknowns' from uncertain matches, merge duplicates, ask the
candidate (email or in-app), turn answers into profile facts, and queue re-scoring."""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.ai.tasks import AITasks
from jobfinder.config import get_settings
from jobfinder.models import ClarifyingQuestion, Job, JobMatch, Profile, ProfileFact
from jobfinder.scheduling import queue

log = logging.getLogger("jobfinder.questions")
UNSURE_BELOW = 0.6  # confidence under this ...
WORTH_ASKING_FROM = 50  # ... on a match scoring at least this: could be good, but we're not sure
OPEN = ("pending", "emailed")
MAX_RAW = 20
MAX_ANSWER_CHARS = 2000


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", s.lower()).strip()


async def open_count(db: AsyncSession, user_id: int) -> int:
    return await db.scalar(
        sa.select(sa.func.count()).select_from(ClarifyingQuestion).where(
            ClarifyingQuestion.user_id == user_id, ClarifyingQuestion.status.in_(OPEN)
        )
    ) or 0


async def collect_questions(db: AsyncSession, ai: AITasks, user_id: int) -> dict:
    """Look at matches whose unknowns haven't been reviewed yet and turn the worthwhile ones into
    a short list of deduplicated questions. No LLM call when there is nothing to review."""
    unsure = sa.and_(JobMatch.confidence < UNSURE_BELOW, JobMatch.llm_score >= WORTH_ASKING_FROM)
    base = sa.and_(JobMatch.user_id == user_id, JobMatch.questions_collected.is_(False))

    # Matches that are confident or clearly poor need no questions: mark them reviewed.
    await db.execute(sa.update(JobMatch).where(base, sa.not_(unsure)).values(questions_collected=True))
    await db.commit()

    if await open_count(db, user_id) >= get_settings().max_open_questions:
        return {"new_questions": 0, "reviewed": 0, "skipped": "too many unanswered questions already"}

    rows = (
        await db.execute(
            sa.select(JobMatch)
            .join(Job, Job.id == JobMatch.job_id)
            .where(base, unsure, JobMatch.status.in_(("new", "saved")), Job.is_active.is_(True))
            .order_by(JobMatch.llm_score.desc())
            .limit(MAX_RAW)
        )
    ).scalars().all()
    if not rows:
        return {"new_questions": 0, "reviewed": 0}

    raw: list[tuple[str, str]] = []
    raw_matches: list[int] = []
    seen: set[str] = set()
    for m in rows:
        for u in m.unknowns or []:
            q = (u.get("question") or "").strip()
            if q and _norm(q) not in seen:
                seen.add(_norm(q))
                raw.append((q, (u.get("why") or "").strip()))
                raw_matches.append(m.id)
    reviewed_ids = [m.id for m in rows]

    created = 0
    if raw:
        facts = [
            (f.question, f.answer)
            for f in await db.scalars(sa.select(ProfileFact).where(ProfileFact.user_id == user_id).order_by(ProfileFact.id))
        ]
        asked = [
            (q.question, q.status)
            for q in await db.scalars(sa.select(ClarifyingQuestion).where(ClarifyingQuestion.user_id == user_id))
        ]
        room = max(0, get_settings().max_open_questions - await open_count(db, user_id))
        result = await ai.consolidate_questions(facts, asked, raw, max_questions=min(room, get_settings().questions_per_email))
        existing = {_norm(q) for q, _ in asked} | {_norm(q) for q, _ in facts}
        for cq in result.questions[:room]:
            text = cq.question.strip()
            if not text or _norm(text) in existing:
                continue
            ids = sorted({raw_matches[i] for i in cq.sources if 0 <= i < len(raw_matches)} or {raw_matches[0]})
            db.add(ClarifyingQuestion(user_id=user_id, question=text[:500], why=cq.why.strip()[:300], match_ids=ids))
            existing.add(_norm(text))
            created += 1

    # Only mark reviewed once the consolidation succeeded (an AI error above leaves them for next time).
    await db.execute(sa.update(JobMatch).where(JobMatch.id.in_(reviewed_ids)).values(questions_collected=True))
    await db.commit()
    return {"new_questions": created, "reviewed": len(reviewed_ids)}


async def list_open(db: AsyncSession, user_id: int) -> list[dict]:
    qs = (
        await db.scalars(
            sa.select(ClarifyingQuestion)
            .where(ClarifyingQuestion.user_id == user_id, ClarifyingQuestion.status.in_(OPEN))
            .order_by(ClarifyingQuestion.id)
        )
    ).all()
    match_ids = {mid for q in qs for mid in q.match_ids or []}
    jobs: dict[int, dict] = {}
    if match_ids:
        for mid, title, company in (
            await db.execute(
                sa.select(JobMatch.id, Job.title, Job.company_name)
                .join(Job, Job.id == JobMatch.job_id)
                .where(JobMatch.id.in_(match_ids), JobMatch.user_id == user_id)
            )
        ).all():
            jobs[mid] = {"match_id": mid, "title": title, "company": company}
    return [
        {"id": q.id, "question": q.question, "why": q.why, "jobs": [jobs[m] for m in (q.match_ids or []) if m in jobs][:3]}
        for q in qs
    ]


async def answer_questions(db: AsyncSession, user_id: int, answers: list[dict]) -> dict:
    """answers: [{id, answer?, skip?}]. Answers become profile facts; the profile version bump makes the
    affected matches stale so the next matching run re-scores them. Unknown or foreign ids are ignored."""
    ids = [a["id"] for a in answers]
    owned = {
        q.id: q
        for q in await db.scalars(
            sa.select(ClarifyingQuestion).where(
                ClarifyingQuestion.id.in_(ids), ClarifyingQuestion.user_id == user_id, ClarifyingQuestion.status.in_(OPEN)
            )
        )
    }
    answered = skipped = 0
    now = datetime.now(UTC)
    for a in answers:
        q = owned.get(a["id"])
        if not q:
            continue
        text = (a.get("answer") or "").strip()[:MAX_ANSWER_CHARS]
        if text:
            q.status, q.answer, q.answered_at = "answered", text, now
            db.add(ProfileFact(user_id=user_id, question=q.question, answer=text, source="job_question"))
            answered += 1
        elif a.get("skip"):
            q.status, q.answered_at = "skipped", now
            skipped += 1
    if answered:
        profile = await db.get(Profile, user_id)
        if profile:
            profile.version += 1
    await db.commit()
    if answered:
        await queue.enqueue(db, "match_user", user_id=user_id, dedupe=True)
    return {"answered": answered, "skipped": skipped, "remaining": await open_count(db, user_id)}
