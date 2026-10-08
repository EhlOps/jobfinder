"""The ongoing profile interview: a recruiter-style audit of the profile that produces a dossier, a
readiness score and the next questions to ask. Answers become profile facts and trigger a fresh audit."""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.ai.tasks import AITasks
from jobfinder.models import Job, JobMatch, Profile, ProfileFact
from jobfinder.scheduling import queue

MAX_ANSWER_CHARS = 3000
DEMAND_LIMIT = 15
SKIPPED_LIMIT = 20


async def demand(db: AsyncSession, user_id: int) -> list[dict]:
    """Requirements that scored jobs asked for and the profile left open, most-requested first."""
    rows = await db.scalars(
        sa.select(JobMatch.requirements)
        .join(Job, Job.id == JobMatch.job_id)
        .where(JobMatch.user_id == user_id, JobMatch.status.in_(("new", "saved")), Job.is_active.is_(True))
    )
    counts: dict[str, dict] = defaultdict(lambda: {"jobs": 0, "must": 0})
    for reqs in rows:
        for r in reqs or []:
            if r.get("status") in ("unknown", "partial", "unmet"):
                key = " ".join((r.get("requirement") or "").lower().split())
                if key:
                    c = counts[key]
                    c.update(requirement=r["requirement"], jobs=c["jobs"] + 1, must=c["must"] + (r.get("importance") == "must"))
    ranked = sorted(counts.values(), key=lambda c: (c["must"], c["jobs"]), reverse=True)
    return [{"requirement": c["requirement"], "jobs": c["jobs"], "required_by": c["must"]} for c in ranked[:DEMAND_LIMIT]]


async def run_audit(db: AsyncSession, ai: AITasks, user_id: int) -> dict:
    profile = await db.get(Profile, user_id)
    if not profile or not (profile.background or profile.status):
        return {"skipped": "profile not set up"}
    facts = [
        (f.question, f.answer)
        for f in await db.scalars(sa.select(ProfileFact).where(ProfileFact.user_id == user_id).order_by(ProfileFact.id))
    ]
    skipped = (profile.audit or {}).get("skipped", [])
    version = profile.version
    result = await ai.audit_profile(profile.status or {}, profile.background or {}, facts, await demand(db, user_id), skipped)
    profile.dossier = result.dossier.model_dump()
    profile.readiness = result.readiness
    profile.audit = {
        "dimensions": result.dimensions,
        "questions": [q.model_dump() for q in result.questions],
        "skipped": skipped,
        "version": version,
        "audited_at": datetime.now(UTC).isoformat(),
    }
    await db.commit()
    # The dossier feeds scoring, so score again with it.
    await queue.enqueue(db, "match_user", user_id=user_id, dedupe=True)
    return {"readiness": result.readiness, "questions": len(result.questions)}


def view(profile: Profile | None) -> dict:
    audit = (profile.audit if profile else None) or {}
    return {
        "readiness": profile.readiness if profile else 0,
        "dimensions": audit.get("dimensions", {}),
        "dossier": (profile.dossier if profile else None) or {},
        "questions": audit.get("questions", []),
        "audited": bool(audit.get("audited_at")),
        "stale": bool(profile) and audit.get("version", 0) < profile.version,
    }


async def answer(db: AsyncSession, user_id: int, answers: list[dict]) -> dict:
    """answers: [{question, answer?, skip?}] (the question text identifies it). Answers become facts
    (source 'interview'); either way the question leaves the list. A new audit is queued."""
    profile = await db.get(Profile, user_id)
    if not profile:
        return {"answered": 0, "skipped": 0}
    audit = dict(profile.audit or {})
    pending = {q["question"] for q in audit.get("questions", [])}
    skipped_list = list(audit.get("skipped", []))
    answered = skipped = 0
    done: set[str] = set()
    for a in answers:
        q = (a.get("question") or "").strip()
        text = (a.get("answer") or "").strip()[:MAX_ANSWER_CHARS]
        if q not in pending:
            continue
        if text:
            db.add(ProfileFact(user_id=user_id, question=q[:2000], answer=text, source="interview"))
            answered += 1
            done.add(q)
        elif a.get("skip"):
            skipped_list.append(q)
            skipped += 1
            done.add(q)
    if done:
        audit["questions"] = [q for q in audit.get("questions", []) if q["question"] not in done]
        audit["skipped"] = skipped_list[-SKIPPED_LIMIT:]
        profile.audit = audit
    if answered:
        profile.version += 1
    await db.commit()
    if done:
        await queue.enqueue(db, "audit_profile", user_id=user_id, dedupe=True)
    return {"answered": answered, "skipped": skipped}
