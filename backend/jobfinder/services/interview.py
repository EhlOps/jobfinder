"""The ongoing profile interview: a recruiter-style audit of the profile that produces a dossier, a
readiness score and the next questions to ask. Answers become profile facts and trigger a fresh audit."""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.integrations.claude.tasks import AITasks
from jobfinder.models import Profile, ProfileFact, Task
from jobfinder.repositories import profiles
from jobfinder.services import profile as profile_service
from jobfinder.tasks import ledger

MAX_ANSWER_CHARS = 3000
DEMAND_LIMIT = 15
SKIPPED_LIMIT = 20
QUEUE_LIMIT = 12


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


async def demand(db: AsyncSession, user_id: int) -> list[dict]:
    """Requirements that scored jobs asked for and the profile left open, most-requested first."""
    rows = await profiles.open_requirements(db, user_id)
    counts: dict[str, dict] = defaultdict(lambda: {"jobs": 0, "must": 0})
    for reqs in rows:
        for r in reqs or []:
            if r.get("status") in ("unknown", "partial", "unmet"):
                key = _norm(r.get("requirement") or "")
                if key:
                    c = counts[key]
                    c.update(requirement=r["requirement"], jobs=c["jobs"] + 1, must=c["must"] + (r.get("importance") == "must"))
    ranked = sorted(counts.values(), key=lambda c: (c["must"], c["jobs"]), reverse=True)
    return [{"requirement": c["requirement"], "jobs": c["jobs"], "required_by": c["must"]} for c in ranked[:DEMAND_LIMIT]]


def _set_up(profile: Profile | None) -> bool:
    return bool(profile and (profile.background or profile.status))


async def request_audit(db: AsyncSession, user_id: int) -> Task:
    """The 'refresh' button: queue an audit (one at a time per user)."""
    if not _set_up(await profiles.get(db, user_id)):
        raise profile_service.ProfileIncomplete("Set up your profile first")
    return await ledger.add(db, "audit_profile", user_id=user_id, dedupe=True)


async def run_audit(db: AsyncSession, ai: AITasks, user_id: int) -> dict:
    """The audit_profile task. Nothing here commits: the handler's boundary does, and with it releases the
    FOR UPDATE lock taken after the AI call (the completion commit follows at once)."""
    profile = await profiles.get(db, user_id)
    if not _set_up(profile):
        return {"skipped": "profile not set up"}
    facts = [(f.question, f.answer) for f in await profiles.facts(db, user_id)]
    skipped = (profile.audit or {}).get("skipped", [])
    queued = [q["question"] for q in (profile.audit or {}).get("questions", [])]
    version = profile.version
    result = await ai.audit_profile(
        profile.status or {}, profile.background or {}, facts, await demand(db, user_id), skipped, queued
    )
    # The audit can take minutes; the user may have answered or skipped meanwhile. Lock the row and merge
    # into the current queue: the question on screen stays first, and the audit may retire the rest.
    await profiles.lock(db, profile)
    current = profile.audit or {}
    live = list(current.get("questions", []))
    skipped_now = {_norm(q) for q in current.get("skipped", [])}
    answered = {_norm(q) for q in await profiles.fact_questions(db, user_id)}
    returned = {_norm(q.question) for q in result.questions}
    head, rest = live[:1], live[1:]
    kept = [q for q in rest if _norm(q["question"]) in returned and _norm(q["question"]) not in answered]
    seen = {_norm(q["question"]) for q in live} | answered | skipped_now
    added = []
    for q in result.questions:
        if _norm(q.question) not in seen:
            seen.add(_norm(q.question))
            added.append(q.model_dump())
    active = [q for q in kept if _norm(q["question"]) not in skipped_now]
    deferred = [q for q in kept if _norm(q["question"]) in skipped_now]
    profile.dossier = result.dossier.model_dump()
    profile.readiness = result.readiness
    profile.audit = {
        "dimensions": result.dimensions,
        "questions": (head + active + added + deferred)[:QUEUE_LIMIT],
        "skipped": current.get("skipped", []),
        "version": version,
        "audited_at": datetime.now(UTC).isoformat(),
    }
    # The dossier feeds scoring, so score again with it.
    await ledger.add(db, "match_user", user_id=user_id, dedupe=True)
    return {"readiness": result.readiness, "questions": len(added)}


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


async def current(db: AsyncSession, user_id: int) -> dict:
    return view(await profiles.get(db, user_id))


async def answer(db: AsyncSession, user_id: int, answers: list[dict]) -> dict:
    """answers: [{question, answer?, skip?}] (the question text identifies it). Answers become facts
    (source 'interview'); either way the question leaves the list. A new audit is queued.
    The row lock keeps a concurrent audit from merging over these answers; the boundary's commit releases it."""
    profile = await profiles.get(db, user_id, for_update=True)
    if not profile:
        return {"answered": 0, "skipped": 0}
    audit = dict(profile.audit or {})
    pending = {q["question"] for q in audit.get("questions", [])}
    skipped_list = list(audit.get("skipped", []))
    answered = skipped = 0
    done: set[str] = set()
    deferred: list[str] = []
    facts: list[ProfileFact] = []
    for a in answers:
        q = (a.get("question") or "").strip()
        text = (a.get("answer") or "").strip()[:MAX_ANSWER_CHARS]
        if q not in pending or q in done:
            continue
        if text:
            facts.append(ProfileFact(user_id=user_id, question=q[:2000], answer=text, source="interview"))
            answered += 1
            done.add(q)
        elif a.get("skip"):
            if q not in skipped_list:
                skipped_list.append(q)
            skipped += 1
            deferred.append(q)
            done.add(q)
    if done:
        by_text = {q["question"]: q for q in audit.get("questions", [])}
        rest = [q for q in audit.get("questions", []) if q["question"] not in done]
        # Skipped questions go to the back of the queue instead of leaving it.
        audit["questions"] = rest + [by_text[q] for q in deferred]
        audit["skipped"] = skipped_list[-SKIPPED_LIMIT:]
        profile.audit = audit
    profiles.add_facts(db, facts)
    if answered:
        profile_service.bump_version(profile)
        await ledger.add(db, "audit_profile", user_id=user_id, dedupe=True)
    return {"answered": answered, "skipped": skipped}
