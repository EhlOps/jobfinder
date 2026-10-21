"""Match one user against the cached jobs: hard filters -> prefilter rank -> LLM score (budgeted)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jobfinder.core.config import settings
from jobfinder.core.db import AsyncSession
from jobfinder.integrations.claude.claude_code import AIError
from jobfinder.integrations.claude.schemas import MatchScore, verdict_for
from jobfinder.integrations.claude.tasks import AITasks
from jobfinder.models import Company, Job, JobMatch, Profile, User
from jobfinder.repositories import matches as matches_repo
from jobfinder.services import profile as profile_service
from jobfinder.services.matching import prefilter as pf
from jobfinder.services.matching.scoring import finalize

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
    limit = user.match_budget if user is not None and user.match_budget is not None else settings.match_daily_llm_budget
    used = await matches_repo.scored_since(db, user_id, datetime.now(UTC) - BUDGET_WINDOW)
    return max(0, limit - used)


def _score_batch(rows: list, descriptions: dict[int, str], status: dict, skills: list[str],
                 acceptable: set[str] | None, needs_sponsorship: bool, threshold: float,
                 stage: str | None = None) -> list[Candidate]:
    """CPU-bound part (regexes over long descriptions); plain data in and out so it can run in a thread."""
    out = []
    for r in rows:
        desc = descriptions.get(r.id, "")
        if needs_sponsorship and pf.refuses_sponsorship(desc):
            continue
        if pf.too_experienced(desc, acceptable) or pf.outside_grad_window(desc, status):
            continue
        s = pf.prefilter_score(
            title=r.title, description=desc, posted_at=r.posted_at, company_tier=r.prestige_tier,
            company_size=r.size, company_industry=r.industry, status=status, skills=skills,
            seniority=r.seniority, stage=stage,
            sponsorship=pf.sponsorship_signal(desc) if needs_sponsorship else None, company_sponsors=r.sponsors_visas,
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
    today = datetime.now(UTC).date()
    acceptable = pf.acceptable_seniorities(status, today)
    stage = pf.career_stage(status, today)
    place = pf.place_prefs(status)
    skills = pf.profile_skills(background)
    needs_sponsorship = status.get("needs_visa_sponsorship") is True
    threshold = settings.match_min_prefilter

    light = [
        r for r in await matches_repo.unmatched_jobs(db, user_id, acceptable)
        if pf.location_ok(r.location, r.workplace_type, place) and pf.salary_ok(status, r.salary_max, r.salary_currency)
    ]
    scored: list[Candidate] = []
    for i in range(0, len(light), BATCH):
        chunk = light[i : i + BATCH]
        descriptions = await matches_repo.descriptions(db, [r.id for r in chunk])
        scored += await asyncio.to_thread(
            _score_batch, chunk, descriptions, status, skills, acceptable, needs_sponsorship, threshold, stage
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
    by_id = {j.id: (j, c) for j, c in await matches_repo.jobs_with_companies(db, [c.job_id for c in candidates])}
    return [(*by_id[c.job_id], c.score, False) for c in candidates if c.job_id in by_id]


async def find_stale(db: AsyncSession, user_id: int, version: int) -> list[tuple[JobMatch, Job, Company | None]]:
    """Matches scored against an older profile version: ones the user answered questions about first,
    then saved ones, then the rest by score (a low score is often just a gap the answers fill). Closed jobs are skipped."""
    answered = await matches_repo.answered_match_ids(db, user_id)
    return await matches_repo.stale(db, user_id, version, STALE_STATUSES, sorted(answered) or [-1])


def stage_info(status: dict, today=None) -> dict | None:
    """What the scorer is told about a student or recent graduate (None for everyone else)."""
    stage = pf.career_stage(status, today)
    if stage is None or stage == pf.GRADUATED:
        return None
    info: dict = {"stage": stage}
    months = pf.months_to_graduation(status, today)
    if months is not None:
        info.update(graduation=status.get("graduation_date"), months_to_graduation=months)
    return info


def sync_career_stage(profile: Profile, today=None) -> bool:
    """Keep profiles.career_stage current. When the stage really changes (the graduation date passed, or a
    student became a final-year), bump the profile version so saved and new matches are scored again.
    The first time a stage is recorded nothing is re-scored. Returns True when the version was bumped."""
    stage = pf.career_stage(profile.status or {}, today) or ""
    previous = profile.career_stage or ""
    if stage == previous:
        return False
    profile.career_stage = stage
    if previous:
        profile_service.bump_version(profile)
        return True
    return False


def sponsorship_info(status: dict, job: Job, company: Company | None) -> dict | None:
    """What the scorer is told about visa sponsorship: only for candidates who need it; unknown fields are left out."""
    if status.get("needs_visa_sponsorship") is not True:
        return None
    info: dict = {"needs_sponsorship": True}
    if status.get("work_authorization"):
        info["work_authorization"] = status["work_authorization"]
    if signal := pf.sponsorship_signal(job.description_text or ""):
        info["posting_says"] = signal
    if company is not None and company.sponsors_visas is not None:
        info["company_sponsors"] = company.sponsors_visas
    return info


async def _score(ai: AITasks, profile: Profile, facts: list[tuple[str, str]], job: Job, company: Company | None):
    extra = {}
    if info := stage_info(profile.status or {}):
        extra["stage_info"] = info
    if sponsorship := sponsorship_info(profile.status or {}, job, company):
        extra["sponsorship"] = sponsorship
    result = await ai.score_match(
        profile.status or {}, profile.background or {}, facts, job_payload(job, company), dossier=profile.dossier or None,
        **extra,
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
    await matches_repo.upsert(db, values)  # a re-score keeps the user's status (saved/applied/...)


Checkpoint = Callable[[], Awaitable[None]]


async def _nothing() -> None:
    pass


async def match_user(
    db: AsyncSession, ai: AITasks, user_id: int, *, budget: int | None = None, checkpoint: Checkpoint | None = None,
) -> dict:
    """Score as many promising jobs as the daily budget allows. Never commits itself: `checkpoint` (the task
    handler passes the session's commit) runs after each scored job and before the summary is returned or the
    AIError re-raised, so a rate limit or crash mid-run loses nothing and the task can retry where it stopped."""
    checkpoint = checkpoint or _nothing
    profile = await db.get(Profile, user_id)
    if not profile or not profile.status:
        return {"scored": 0, "pending": 0, "skipped": "profile not set up"}
    facts = await matches_repo.facts(db, user_id)
    if sync_career_stage(profile):
        await checkpoint()
    version = profile.version
    left = await remaining_budget(db, user_id) if budget is None else budget

    # (job, company, prefilter, is_rescore): stale re-scores first, then new candidates, as many as the budget allows
    stale = await find_stale(db, user_id, version)
    work: list[tuple[Job, Company | None, float, bool]] = [(j, c, m.prefilter_score, True) for m, j, c in stale][:left]
    candidates = await find_candidates(db, user_id, profile)
    total_work, batch_size = len(stale) + len(candidates), max(1, settings.ai_max_concurrency)
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
                await checkpoint()
                scored += 1
        if fatal:
            break

    summary = {
        "last_run_at": datetime.now(UTC).isoformat(), "scored": scored,
        "pending": max(0, total_work - scored - failed), "failed": failed,
    }
    profile.match_summary = summary
    await checkpoint()
    if fatal:
        raise fatal
    return summary
