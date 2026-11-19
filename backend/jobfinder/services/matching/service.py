"""Match one user against the cached jobs: retrieval -> hard filters -> relevance rank -> LLM score, one budgeted
slice at a time.

Retrieval (MATCH_RETRIEVAL) picks the jobs worth Claude's time: `prefilter` scores every unmatched job with the
heuristic prefilter and keeps the ones over MATCH_MIN_PREFILTER; `vector` (#126) takes the profile's nearest
postings by embedding (EMBED_MODEL) and keeps them all, closest first, once the hard filters pass them; `hybrid`
keeps the prefilter's jobs but orders them by a blend of both scores (`Blend`). Every Claude score also leaves a
`match_reviews` label saying which engine picked the job and where it ranked."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import fmean, pstdev
from typing import NamedTuple

from jobfinder.core.config import settings
from jobfinder.core.db import AsyncSession
from jobfinder.integrations.claude.claude_code import AIError
from jobfinder.integrations.claude.schemas import MatchScore, verdict_for
from jobfinder.integrations.claude.tasks import AITasks
from jobfinder.models import Company, Job, Profile, User
from jobfinder.repositories import matches as matches_repo
from jobfinder.repositories import retrieval as retrieval_repo
from jobfinder.services import profile as profile_service
from jobfinder.services.matching import prefilter as pf
from jobfinder.services.matching.scoring import finalize

log = logging.getLogger("jobfinder.services.matching")
BUDGET_WINDOW = timedelta(hours=24)
STALE_STATUSES = ("new", "saved")  # dismissed/applied matches are never re-scored
UNLIMITED = 10**9  # remaining budget for users who turned the daily limit off


BATCH = 500  # jobs whose descriptions are loaded and scored at a time
PREFILTER, VECTOR, HYBRID = "prefilter", "vector", "hybrid"  # the engines (MATCH_RETRIEVAL), as match_reviews.engine


@dataclass
class Candidate:
    """A job to score: one that passed the filters, or a stale match to re-score (`match_id` set).
    Deliberately light (no description); `hydrate` loads the full rows for the few that actually get scored."""
    job_id: int
    score: float
    posted_ts: float
    dedupe_hash: str
    match_id: int | None = None
    saved: bool = False
    retrieval_score: float | None = None  # max cosine to the profile's query vectors (vector and hybrid engines)
    blend: float | None = None  # the hybrid engine's ordering score (Blend)


class Work(NamedTuple):
    """A hydrated candidate: what `_score` and `_save` need. `rank` is its 0-based place in the run's ranked list."""
    job: Job
    company: Company | None
    prefilter: float
    rescore: bool
    rank: int
    retrieval_score: float | None


@dataclass
class Retrieval:
    """What `retrieve` picked, and how: `engine` is the one actually used (vector and hybrid fall back to
    prefilter)."""
    engine: str
    candidates: list[Candidate]

    @property
    def model(self) -> str | None:
        return settings.embed_model if self.engine in (VECTOR, HYBRID) else None


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
    """Match scores the user has left in the rolling 24h window. The cap is on bulk scoring only: it counts
    `job_matches.scored_at`, so profile extracts, audits, letters and resumes never spend it."""
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
    """The jobs worth scoring, best first, one per posting (dedupe_hash), excluding anything already
    matched for this user (`retrieve`)."""
    return (await retrieve(db, user_id, profile)).candidates


async def retrieve(db: AsyncSession, user_id: int, profile: Profile, *, include_matched: bool = False,
                   stale: list[Candidate] | None = None) -> Retrieval:
    """Candidates by the configured engine (MATCH_RETRIEVAL). `vector` and `hybrid` need the current profile
    version's query vectors for EMBED_MODEL; until the indexing sweep has made them, this run uses the prefilter.
    `hybrid` also uses the prefilter for a profile without a background (no skills or summary): its query
    vectors are bare job titles, too weak to reorder by. `stale` (match_user's re-scores) get the hybrid
    engine's blend too, so rank_work can order them with the new jobs. `include_matched` keeps already-matched
    jobs (only `jobfinder ops eval-retrieval` wants them)."""
    engine = settings.match_retrieval
    if engine == HYBRID and not has_background(profile):
        log.info("user %s: no background skills or summary, retrieving by prefilter", user_id)
    elif engine in (VECTOR, HYBRID):
        if await retrieval_repo.has_query_vectors(db, user_id, profile.version, settings.embed_model):
            if engine == HYBRID:
                return Retrieval(HYBRID, await hybrid_candidates(db, user_id, profile, include_matched=include_matched,
                                                                 stale=stale))
            return Retrieval(VECTOR, await vector_candidates(db, user_id, profile, include_matched=include_matched))
        log.info("user %s: profile version %s has no %s query vectors yet, retrieving by prefilter",
                 user_id, profile.version, settings.embed_model)
    return Retrieval(PREFILTER, await prefilter_candidates(db, user_id, profile, include_matched=include_matched))


def has_background(profile: Profile) -> bool:
    """Whether the profile's query vectors carry more than its target roles (what profile_queries embeds)."""
    background = profile.background or {}
    return bool(pf.profile_skills(background) or background.get("summary"))


@dataclass
class _Filters:
    """The profile's hard filters and prefilter inputs, worked out once per run."""
    status: dict
    acceptable: set[str] | None
    stage: str | None
    place: pf.Place
    skills: list[str]
    needs_sponsorship: bool

    @classmethod
    def of(cls, profile: Profile) -> _Filters:
        status, background = profile.status or {}, profile.background or {}
        today = datetime.now(UTC).date()
        return cls(status, pf.acceptable_seniorities(status, today), pf.career_stage(status, today),
                   pf.place_prefs(status), pf.profile_skills(background), status.get("needs_visa_sponsorship") is True)

    async def apply(self, db: AsyncSession, rows: list, threshold: float) -> list[Candidate]:
        """Place and pay on the light rows, then sponsorship, years and graduation window on the descriptions
        (loaded BATCH at a time, checked off the event loop), keeping prefilter scores >= `threshold`."""
        light = [
            r for r in rows
            if pf.location_ok(r.location, r.workplace_type, self.place)
            and pf.salary_ok(self.status, r.salary_max, r.salary_currency)
        ]
        out: list[Candidate] = []
        for i in range(0, len(light), BATCH):
            chunk = light[i : i + BATCH]
            descriptions = await matches_repo.descriptions(db, [r.id for r in chunk])
            out += await asyncio.to_thread(
                _score_batch, chunk, descriptions, self.status, self.skills, self.acceptable, self.needs_sponsorship,
                threshold, self.stage,
            )
        return out


def _one_per_posting(ordered: list[Candidate]) -> list[Candidate]:
    out, seen_hashes = [], set()
    for c in ordered:
        if c.dedupe_hash not in seen_hashes:
            seen_hashes.add(c.dedupe_hash)
            out.append(c)
    return out


async def prefilter_candidates(db: AsyncSession, user_id: int, profile: Profile, *,
                               include_matched: bool = False) -> list[Candidate]:
    """Every active unmatched job that passes the hard filters and the prefilter threshold, by prefilter score,
    then newest posting.

    Works in two passes so memory stays flat as the job cache grows: a light query (no descriptions)
    applies the cheap filters, then descriptions are loaded BATCH at a time for the rest and scored off
    the event loop."""
    f = _Filters.of(profile)
    rows = await matches_repo.unmatched_jobs(db, user_id, f.acceptable, include_matched=include_matched)
    scored = await f.apply(db, rows, settings.match_min_prefilter)
    return _one_per_posting(sorted(scored, key=lambda c: (-c.score, -c.posted_ts)))


async def vector_candidates(db: AsyncSession, user_id: int, profile: Profile, *,
                            include_matched: bool = False) -> list[Candidate]:
    """The MATCH_RETRIEVAL_K nearest postings per query vector that pass the hard filters, closest first (then
    newest). The prefilter score is computed for storage only: no MATCH_MIN_PREFILTER cutoff, the closest
    jobs are the point."""
    f = _Filters.of(profile)
    rows = await retrieval_repo.nearest_unmatched_jobs(
        db, user_id, profile.version, settings.embed_model, settings.match_retrieval_k, f.acceptable,
        include_matched=include_matched,
    )
    cosine = {r.id: r.retrieval_score for r in rows}
    scored = await f.apply(db, rows, float("-inf"))
    for c in scored:
        c.retrieval_score = cosine[c.job_id]
    return _one_per_posting(sorted(scored, key=lambda c: (-c.retrieval_score, -c.posted_ts)))


@dataclass(frozen=True)
class Blend:
    """The hybrid engine's ordering score: w * z(cosine) + (1 - w) * z(prefilter), w = MATCH_HYBRID_WEIGHT. Each
    score is z-scored with the mean and sd of the jobs being ranked, so neither scale dominates. A job without a
    vector yet (newly ingested, before embed_sweep reaches it) gets z(cosine) = 0: exactly average."""
    weight: float
    pre: tuple[float, float]  # mean, sd
    cos: tuple[float, float]

    @classmethod
    def fit(cls, candidates: list[Candidate], weight: float) -> Blend:
        return cls(min(1.0, max(0.0, weight)), _mean_sd([c.score for c in candidates]),
                   _mean_sd([c.retrieval_score for c in candidates if c.retrieval_score is not None]))

    def __call__(self, c: Candidate) -> float:
        z_pre = (c.score - self.pre[0]) / self.pre[1]
        z_cos = 0.0 if c.retrieval_score is None else (c.retrieval_score - self.cos[0]) / self.cos[1]
        return self.weight * z_cos + (1 - self.weight) * z_pre


def _mean_sd(xs: list[float]) -> tuple[float, float]:
    if not xs:
        return 0.0, 1.0
    return fmean(xs), pstdev(xs) or 1.0


async def hybrid_candidates(db: AsyncSession, user_id: int, profile: Profile, *, include_matched: bool = False,
                            stale: list[Candidate] | None = None) -> list[Candidate]:
    """The prefilter's candidates (same hard filters, same MATCH_MIN_PREFILTER cutoff), each with its exact
    cosine to the profile, ordered by `Blend` (then newest posting). The blend is fitted over the candidates and
    `stale` together, and set on both."""
    pool = await prefilter_candidates(db, user_id, profile, include_matched=include_matched)
    everything = pool + (stale or [])
    cosine = await retrieval_repo.pool_cosines(db, user_id, profile.version, settings.embed_model,
                                               [c.job_id for c in everything])
    for c in everything:
        c.retrieval_score = cosine.get(c.job_id)
    blend = Blend.fit(everything, settings.match_hybrid_weight)
    for c in everything:
        c.blend = blend(c)
    return sorted(pool, key=lambda c: (-c.blend, -c.posted_ts))


async def hydrate(db: AsyncSession, candidates: list[Candidate]) -> list[Work]:
    """Load the full job and company rows for the candidates that will actually be scored (order kept).
    `candidates` is the head of the ranked list, so a candidate's index in it is its rank."""
    if not candidates:
        return []
    by_id = {j.id: (j, c) for j, c in await matches_repo.jobs_with_companies(db, [c.job_id for c in candidates])}
    return [
        Work(*by_id[c.job_id], c.score, c.match_id is not None, rank, c.retrieval_score)
        for rank, c in enumerate(candidates) if c.job_id in by_id
    ]


async def find_stale(db: AsyncSession, user_id: int, version: int) -> list[Candidate]:
    """Matches scored against an older profile version, as light candidates (closed jobs are skipped)."""
    return [
        Candidate(r.job_id, r.prefilter_score or 0.0, r.posted_at.timestamp() if r.posted_at else 0, r.dedupe_hash,
                  match_id=r.match_id, saved=r.status == "saved")
        for r in await matches_repo.stale(db, user_id, version, STALE_STATUSES)
    ]


def rank_work(stale: list[Candidate], candidates: list[Candidate], answered: set[int],
              engine: str = PREFILTER) -> list[Candidate]:
    """One relevance-ordered list of everything worth scoring, instead of "every stale re-score first, then
    new jobs". Tiers: stale matches the user answered questions about, then saved ones (both are on screen and
    the user is waiting on them), then everything else, stale and new together. A low old score is no reason
    to wait behind new jobs: it is often just a gap the answers fill.

    Tiers 0 and 1 (stale only) go by stored prefilter score, then newest posting. In tier 2:
    - prefilter engine: stale and new together by prefilter score, then newest posting;
    - hybrid engine: the same, by the blend (`retrieve` set it on both) instead of the prefilter score;
    - vector engine: the two scores aren't comparable (cosine vs prefilter), so the lists are interleaved by
      position: new candidates keep `retrieve`'s order (closest first), stale ones their stored order (prefilter
      score, then newest), and at each position the new candidate goes first.
    No posting appears twice: candidates already exclude every matched posting (unmatched_jobs)."""
    def tier(w: Candidate) -> int:
        return 0 if w.match_id in answered else 1 if w.saved else 2

    if engine == HYBRID:
        return sorted(stale + candidates, key=lambda w: (
            tier(w), -(w.blend if tier(w) == 2 and w.blend is not None else w.score), -w.posted_ts))
    if engine != VECTOR:
        return sorted(stale + candidates, key=lambda w: (tier(w), -w.score, -w.posted_ts))
    pos: dict[int, int] = {}  # id(work) -> its position within its tier, in its own list's order
    for ws in (sorted(stale, key=lambda w: (tier(w), -w.score, -w.posted_ts)), candidates):
        seen: Counter[int] = Counter()
        for w in ws:
            pos[id(w)] = seen[tier(w)]
            seen[tier(w)] += 1
    return sorted(stale + candidates, key=lambda w: (tier(w), pos[id(w)], w.match_id is not None))


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


async def _save(db: AsyncSession, user_id: int, version: int, work: Work, retrieval: Retrieval,
                result: MatchScore) -> None:
    """Store the assessment on the match, and append its label to match_reviews."""
    verdict = verdict_for(result.score)
    requirements = [r.model_dump() for r in result.requirements]
    values = {
        "user_id": user_id, "job_id": work.job.id, "profile_version": version, "prefilter_score": work.prefilter,
        "llm_score": result.score, "confidence": result.confidence, "verdict": verdict,
        "reasons": result.reasons[:4], "gaps": result.gaps[:4], "unknowns": [u.model_dump() for u in result.unknowns[:6]],
        "requirements": requirements, "hire_verdict": result.hire_verdict,
        "recruiter_take": result.recruiter_take[:300],
        "scored_at": datetime.now(UTC), "questions_collected": False,  # new assessment: look at its unknowns again
    }
    await matches_repo.upsert(db, values)  # a re-score keeps the user's status (saved/applied/...)
    await retrieval_repo.add_review(db, {
        "user_id": user_id, "job_id": work.job.id, "profile_version": version,
        "engine": retrieval.engine, "model": retrieval.model,
        "retrieval_rank": work.rank, "retrieval_score": work.retrieval_score, "prefilter_score": work.prefilter,
        "llm_score": result.score, "confidence": result.confidence, "verdict": verdict,
        "requirements": requirements, "scored_at": values["scored_at"],
    })


Checkpoint = Callable[[], Awaitable[None]]


async def _nothing() -> None:
    pass


async def match_user(
    db: AsyncSession, ai: AITasks, user_id: int, *, budget: int | None = None, limit: int | None = None,
    checkpoint: Checkpoint | None = None,
) -> dict:
    """Score one slice: the most relevant jobs (rank_work), at most `limit` of them (the task handler passes
    MATCH_SLICE_JOBS) and at most what the daily budget allows. The budget is re-read every slice, so it holds
    across a chain and an admin lowering it stops the chain at the next slice.

    Never commits itself: `checkpoint` (the task handler passes the session's commit) runs after each scored
    job and before the summary is returned or the AIError re-raised, so a rate limit or crash mid-slice loses
    nothing and the task can retry where it stopped.

    The summary says what's left: `pending` is the work still unscored after this slice (not counting the
    jobs whose answer couldn't be parsed: those wait for the next run, not the next slice) and `budget_left`
    what the budget allows after it; the handler chains the next slice from those."""
    checkpoint = checkpoint or _nothing
    profile = await db.get(Profile, user_id)
    if not profile or not profile.status:
        return {"scored": 0, "pending": 0, "budget_left": 0, "skipped": "profile not set up"}
    facts = await matches_repo.facts(db, user_id)
    if sync_career_stage(profile):
        await checkpoint()
    version = profile.version
    left = await remaining_budget(db, user_id) if budget is None else budget

    # Ranked afresh every slice, so jobs that arrived since the last one compete right away.
    answered = await matches_repo.answered_match_ids(db, user_id)
    stale = await find_stale(db, user_id, version)
    retrieval = await retrieve(db, user_id, profile, stale=stale)
    ranked = rank_work(stale, retrieval.candidates, answered, retrieval.engine)
    take = min(left, limit) if limit is not None else left
    work = await hydrate(db, ranked[: max(0, take)])  # only what gets scored is loaded in full
    batch_size = max(1, settings.ai_max_concurrency)

    scored = failed = 0
    fatal: AIError | None = None
    for i in range(0, len(work), batch_size):
        batch = work[i : i + batch_size]
        results = await asyncio.gather(*(_score(ai, profile, facts, w.job, w.company) for w in batch),
                                       return_exceptions=True)
        for w, res in zip(batch, results, strict=True):
            job = w.job
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
                await _save(db, user_id, version, w, retrieval, res)
                await checkpoint()
                scored += 1
        if fatal:
            break

    # Written every slice, not only when the chain ends: the matches page shows progress, and the planner's
    # "new jobs since last_run_at" stays right because each slice ranked every job seen before it. The
    # planner never plans a user mid-chain anyway: the queued next slice makes them in flight.
    summary = {
        "last_run_at": datetime.now(UTC).isoformat(), "scored": scored, "failed": failed,
        "pending": max(0, len(ranked) - scored - failed),
        # Re-read, not left - scored: re-scoring a match already scored inside the window spends nothing.
        "budget_left": await remaining_budget(db, user_id) if budget is None else max(0, budget - scored),
    }
    profile.match_summary = summary
    await checkpoint()
    if fatal:
        raise fatal
    return summary
