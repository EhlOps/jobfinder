"""Match one user against the cached jobs: hard filters -> prefilter rank -> LLM score (budgeted)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.ai.claude_code import AIError
from jobfinder.ai.schemas import MatchScore, verdict_for
from jobfinder.ai.tasks import AITasks
from jobfinder.config import get_settings
from jobfinder.matching import prefilter as pf
from jobfinder.matching.scoring import finalize
from jobfinder.models import ClarifyingQuestion, Company, Job, JobMatch, Profile, ProfileFact, User
from jobfinder.scheduling import queue

log = logging.getLogger("jobfinder.matching")
BUDGET_WINDOW = timedelta(hours=24)
STALE_STATUSES = ("new", "saved")  # dismissed/applied matches are never re-scored
UNLIMITED = 10**9  # remaining budget for users who turned the daily limit off


BATCH = 500  # jobs whose descriptions are loaded and scored at a time


@dataclass
class Candidate:
    """A job that passed the filters. Deliberately light (no description); `hydrate` loads the full rows
    for the few that actually get scored."""
    job_id: int
    score: float
    posted_ts: float
    dedupe_hash: str


def job_payload(job: Job, company: Company | None) -> dict:
    salary = None
    if job.salary_min or job.salary_max:
        salary = f"{job.salary_min or '?'}-{job.salary_max or '?'} {job.salary_currency or 'USD'} per year"
    return {
        "title": job.title, "company": job.company_name, "location": job.location,
        "workplace_type": job.workplace_type, "seniority_hint": job.seniority, "salary": salary,
        "company_size": company.size if company else None,
        "company_industry": company.industry if company else None,
        "description": job.description_text,
    }


async def remaining_budget(db: AsyncSession, user_id: int) -> int:
    user = await db.get(User, user_id)
    if user is not None and user.match_budget_enabled is False:
        return UNLIMITED
    limit = user.match_budget if user is not None and user.match_budget is not None else get_settings().match_daily_llm_budget
    used = await db.scalar(
        sa.select(sa.func.count()).select_from(JobMatch).where(
            JobMatch.user_id == user_id, JobMatch.scored_at >= datetime.now(UTC) - BUDGET_WINDOW
        )
    )
    return max(0, limit - (used or 0))


def _score_batch(rows: list, descriptions: dict[int, str], status: dict, skills: list[str],
                 acceptable: set[str] | None, needs_sponsorship: bool, threshold: float) -> list[Candidate]:
    """CPU-bound part (regexes over long descriptions); plain data in and out so it can run in a thread."""
    out = []
    for r in rows:
        desc = descriptions.get(r.id, "")
        if needs_sponsorship and pf.refuses_sponsorship(desc):
            continue
        if pf.too_experienced(desc, acceptable):
            continue
        s = pf.prefilter_score(
            title=r.title, description=desc, posted_at=r.posted_at, company_tier=r.prestige_tier,
            company_size=r.size, company_industry=r.industry, status=status, skills=skills,
        )
        if s >= threshold:
            out.append(Candidate(r.id, s, r.posted_at.timestamp() if r.posted_at else 0, r.dedupe_hash))
    return out


async def find_candidates(db: AsyncSession, user_id: int, profile: Profile) -> list[Candidate]:
    """Active jobs that pass the hard filters and the prefilter threshold, best first, one per
    posting (dedupe_hash), excluding anything already matched for this user.

    Works in two passes so memory stays flat as the job cache grows: a light query (no descriptions)
    applies the cheap filters, then descriptions are loaded BATCH at a time for the rest and scored off
    the event loop."""
    status, background = profile.status or {}, profile.background or {}
    acceptable = pf.acceptable_seniorities(status)
    place = pf.place_prefs(status)
    skills = pf.profile_skills(background)
    needs_sponsorship = status.get("needs_visa_sponsorship") is True
    threshold = get_settings().match_min_prefilter

    matched_jobs = sa.select(JobMatch.job_id).where(JobMatch.user_id == user_id)
    matched_hashes = (
        sa.select(Job.dedupe_hash).join(JobMatch, JobMatch.job_id == Job.id).where(JobMatch.user_id == user_id)
    )
    q = (
        sa.select(
            Job.id, Job.title, Job.location, Job.workplace_type, Job.salary_max, Job.salary_currency,
            Job.posted_at, Job.dedupe_hash,
            Company.prestige_tier.label("prestige_tier"), Company.size.label("size"), Company.industry.label("industry"),
        )
        .outerjoin(Company, Company.id == Job.company_id)
        .where(Job.is_active.is_(True), Job.id.not_in(matched_jobs), Job.dedupe_hash.not_in(matched_hashes))
    )
    if acceptable is not None:
        q = q.where(sa.or_(Job.seniority.is_(None), Job.seniority.in_(acceptable)))

    light = [
        r for r in (await db.execute(q)).all()
        if pf.location_ok(r.location, r.workplace_type, place) and pf.salary_ok(status, r.salary_max, r.salary_currency)
    ]
    scored: list[Candidate] = []
    for i in range(0, len(light), BATCH):
        chunk = light[i : i + BATCH]
        descriptions = dict(
            (await db.execute(sa.select(Job.id, Job.description_text).where(Job.id.in_([r.id for r in chunk])))).all()
        )
        scored += await asyncio.to_thread(
            _score_batch, chunk, descriptions, status, skills, acceptable, needs_sponsorship, threshold
        )
    scored.sort(key=lambda c: (-c.score, -c.posted_ts))
    out, seen_hashes = [], set()
    for c in scored:  # one row per distinct posting
        if c.dedupe_hash not in seen_hashes:
            seen_hashes.add(c.dedupe_hash)
            out.append(c)
    return out


async def hydrate(db: AsyncSession, candidates: list[Candidate]) -> list[tuple[Job, Company | None, float, bool]]:
    """Load the full job and company rows for the candidates that will actually be scored (order kept)."""
    if not candidates:
        return []
    rows = (
        await db.execute(
            sa.select(Job, Company).outerjoin(Company, Company.id == Job.company_id)
            .where(Job.id.in_([c.job_id for c in candidates]))
        )
    ).all()
    by_id = {j.id: (j, c) for j, c in rows}
    return [(*by_id[c.job_id], c.score, False) for c in candidates if c.job_id in by_id]


async def find_stale(db: AsyncSession, user_id: int, version: int) -> list[tuple[JobMatch, Job, Company | None]]:
    """Matches scored against an older profile version: ones the user answered questions about first,
    then saved ones, then the rest by score (a low score is often just a gap the answers fill). Closed jobs are skipped."""
    answered = await _answered_match_ids(db, user_id)
    rows = (
        await db.execute(
            sa.select(JobMatch, Job, Company)
            .join(Job, Job.id == JobMatch.job_id)
            .outerjoin(Company, Company.id == Job.company_id)
            .where(
                JobMatch.user_id == user_id, JobMatch.profile_version < version,
                JobMatch.status.in_(STALE_STATUSES), Job.is_active.is_(True),
            )
            .order_by(JobMatch.id.in_(answered).desc(), (JobMatch.status == "saved").desc(), JobMatch.llm_score.desc())
        )
    ).all()
    return [(m, j, c) for m, j, c in rows]


async def _answered_match_ids(db: AsyncSession, user_id: int) -> list[int]:
    """Matches linked to clarifying questions the user has answered: re-score these first."""
    rows = await db.scalars(
        sa.select(ClarifyingQuestion.match_ids).where(
            ClarifyingQuestion.user_id == user_id, ClarifyingQuestion.status == "answered"
        )
    )
    ids = {mid for found in rows for mid in found or []}
    # ...and matches for jobs the user answered a question about directly on the job page
    ids |= set(
        await db.scalars(
            sa.select(JobMatch.id)
            .join(ProfileFact, ProfileFact.job_id == JobMatch.job_id)
            .where(JobMatch.user_id == user_id, ProfileFact.user_id == user_id, ProfileFact.source == "job_question")
        )
    )
    return sorted(ids) or [-1]


async def _score(ai: AITasks, profile: Profile, facts: list[tuple[str, str]], job: Job, company: Company | None):
    result = await ai.score_match(
        profile.status or {}, profile.background or {}, facts, job_payload(job, company), dossier=profile.dossier or None
    )
    return finalize(result)


async def _save(db: AsyncSession, user_id: int, job_id: int, version: int, prefilter: float, result: MatchScore) -> None:
    values = {
        "user_id": user_id, "job_id": job_id, "profile_version": version, "prefilter_score": prefilter,
        "llm_score": result.score, "confidence": result.confidence, "verdict": verdict_for(result.score),
        "reasons": result.reasons[:4], "gaps": result.gaps[:4], "unknowns": [u.model_dump() for u in result.unknowns[:6]],
        "requirements": [r.model_dump() for r in result.requirements], "hire_verdict": result.hire_verdict,
        "recruiter_take": result.recruiter_take[:300],
        "scored_at": datetime.now(UTC), "questions_collected": False,  # new assessment: look at its unknowns again
    }
    stmt = insert(JobMatch).values(**values)
    # Re-scoring updates the assessment but keeps the user's status (saved/applied/...).
    await db.execute(
        stmt.on_conflict_do_update(
            constraint="uq_match_user_job", set_={k: stmt.excluded[k] for k in values if k not in ("user_id", "job_id")}
        )
    )
    await db.commit()


async def match_user(db: AsyncSession, ai: AITasks, user_id: int, *, budget: int | None = None) -> dict:
    """Score as many promising jobs as the daily budget allows. Progress is committed per job, so
    a rate limit or crash mid-run loses nothing; the AIError is re-raised so the task can retry."""
    profile = await db.get(Profile, user_id)
    if not profile or not profile.status:
        return {"scored": 0, "pending": 0, "skipped": "profile not set up"}
    facts = [
        (f.question, f.answer)
        for f in (await db.scalars(sa.select(ProfileFact).where(ProfileFact.user_id == user_id).order_by(ProfileFact.id)))
    ]
    version = profile.version
    left = await remaining_budget(db, user_id) if budget is None else budget

    # (job, company, prefilter, is_rescore): stale re-scores first, then new candidates, as many as the budget allows
    stale = await find_stale(db, user_id, version)
    work: list[tuple[Job, Company | None, float, bool]] = [(j, c, m.prefilter_score, True) for m, j, c in stale][:left]
    candidates = await find_candidates(db, user_id, profile)
    total_work, batch_size = len(stale) + len(candidates), max(1, get_settings().ai_max_concurrency)
    work += await hydrate(db, candidates[: max(0, left - len(work))])

    scored = failed = 0
    fatal: AIError | None = None
    for i in range(0, len(work), batch_size):
        batch = work[i : i + batch_size]
        results = await asyncio.gather(*(_score(ai, profile, facts, j, c) for j, c, _, _ in batch), return_exceptions=True)
        for (job, _c, pre, _r), res in zip(batch, results, strict=True):
            if isinstance(res, AIError):
                if res.kind == "bad_output":
                    failed += 1  # one unparseable answer shouldn't stop the run
                    log.warning("job %s: unparseable score, skipped", job.id)
                else:
                    fatal = fatal or res
            elif isinstance(res, Exception):
                failed += 1
                log.exception("scoring job %s failed", job.id, exc_info=res)
            else:
                await _save(db, user_id, job.id, version, pre, res)
                scored += 1
        if fatal:
            break

    summary = {
        "last_run_at": datetime.now(UTC).isoformat(), "scored": scored,
        "pending": max(0, total_work - scored - failed), "failed": failed,
    }
    profile.match_summary = summary
    await db.commit()
    if fatal:
        raise fatal
    return summary


async def enqueue_for_active_users(db: AsyncSession) -> int:
    """After ingestion: queue a matching run for everyone who has finished the questionnaire."""
    ids = (await db.scalars(sa.select(Profile.user_id).where(Profile.status != {}))).all()
    for uid in ids:
        await queue.enqueue(db, "match_user", user_id=uid, dedupe=True)
    return len(ids)
