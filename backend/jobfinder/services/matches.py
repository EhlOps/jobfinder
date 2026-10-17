"""Match rules: the feed, one match, its status, answers given on a job's page, and a re-score request.

Also the two checks the cover letter and resume services share: the match must be the user's
(`owned`), and their profile must have a background to write from (`require_background`).
"""
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.core.errors import Invalid, NotFound
from jobfinder.matching import prefilter
from jobfinder.models import Company, Job, JobMatch, Profile
from jobfinder.repositories import letters as letters_repo
from jobfinder.repositories import matches as repo
from jobfinder.schemas.common import TaskRef
from jobfinder.schemas.matches import JobOut, MatchDetail, MatchList, MatchOut
from jobfinder.services import profile as profile_service
from jobfinder.tasks import ledger

FeedQuery = repo.FeedQuery  # the router builds one from the query string


class ProfileIncomplete(Invalid):
    code, message = "profile_incomplete", "Finish your profile first (onboarding)"


class SearchNotSetUp(Invalid):
    code, message = "search_not_set_up", "Tell us about your search first (onboarding)"


async def owned(db: AsyncSession, user_id: int, match_id: int, message: str | None = None) -> tuple[JobMatch, Job, Company | None]:
    """The user's match, its job and the job's company; someone else's match is NotFound like a missing one."""
    row = await repo.get_owned(db, user_id, match_id)
    if row is None:
        raise NotFound(message)
    return row


def require_background(profile: Profile | None) -> Profile:
    """Letters and resumes are written from the profile's background; without one there is nothing to say."""
    if not profile or not profile.background:
        raise ProfileIncomplete()
    return profile


def sponsorship_value(j: Job, company_sponsors: bool | None) -> str | None:
    """The posting's own words win; a silent posting falls back to what is known about the company."""
    return prefilter.sponsorship_signal(j.description_text or "") or {True: "likely", False: "unlikely"}.get(company_sponsors)


def _job_out(j: Job, company_sponsors: bool | None) -> JobOut:
    return JobOut(
        id=j.id, title=j.title, company=j.company_name, location=j.location, workplace_type=j.workplace_type,
        seniority=j.seniority, salary_min=j.salary_min, salary_max=j.salary_max,
        salary_currency=j.salary_currency, url=j.url, posted_at=j.posted_at, is_active=j.is_active,
        sponsorship=sponsorship_value(j, company_sponsors),
    )


def _out(m: JobMatch, j: Job, version: int, has_letter: bool, company_sponsors: bool | None) -> dict:
    return {
        "id": m.id, "status": m.status, "score": m.llm_score, "confidence": m.confidence,
        "verdict": m.verdict, "reasons": m.reasons, "gaps": m.gaps,
        "hire_verdict": m.hire_verdict, "recruiter_take": m.recruiter_take,
        "stale": m.profile_version < version, "has_cover_letter": has_letter, "scored_at": m.scored_at,
        "job": _job_out(j, company_sponsors),
    }


def _version(profile: Profile | None) -> int:
    return profile.version if profile else 1


async def feed(db: AsyncSession, user_id: int, query: FeedQuery) -> MatchList:
    """One page of the feed, with per-tab counts and the last matching run's summary."""
    profile = await db.get(Profile, user_id)
    counts, total, page = await repo.feed(db, user_id, query)
    with_letters = await letters_repo.job_ids_with_letters(db, user_id, [j.id for _, j, _ in page])
    version = _version(profile)
    return MatchList(
        items=[MatchOut(**_out(m, j, version, j.id in with_letters, sponsors)) for m, j, sponsors in page],
        total=total, counts=counts,
        summary={
            **((profile.match_summary if profile else {}) or {}),
            "career_stage": prefilter.career_stage((profile.status if profile else {}) or {}),
        },
    )


async def _view(db: AsyncSession, user_id: int, m: JobMatch, j: Job, c: Company | None) -> dict:
    profile = await db.get(Profile, user_id)
    has_letter = j.id in await letters_repo.job_ids_with_letters(db, user_id, [j.id])
    return _out(m, j, _version(profile), has_letter, c.sponsors_visas if c else None)


async def detail(db: AsyncSession, user_id: int, match_id: int) -> MatchDetail:
    m, j, c = await owned(db, user_id, match_id)
    return MatchDetail(
        **await _view(db, user_id, m, j, c),
        unknowns=m.unknowns, requirements=m.requirements or [], description=j.description_text,
    )


async def set_status(db: AsyncSession, user_id: int, match_id: int, status: str) -> MatchOut:
    m, j, c = await owned(db, user_id, match_id)
    m.status = status
    return MatchOut(**await _view(db, user_id, m, j, c))


async def answer(db: AsyncSession, user_id: int, match_id: int, answers: list[tuple[str, str]]) -> dict:
    """Answers to a job's requirement questions become profile facts; the profile changed, so every
    match is re-scored (the worker takes the answered ones first)."""
    _, j, _ = await owned(db, user_id, match_id)
    repo.add_job_answers(db, user_id, j.id, answers)
    profile = await db.get(Profile, user_id)
    if profile:
        profile_service.bump_version(profile)
    await ledger.add(db, "match_user", user_id=user_id, dedupe=True)
    return {"saved": len(answers)}


async def refresh(db: AsyncSession, user_id: int) -> TaskRef:
    """Queue a matching run now; there is nothing to match against until the search is described."""
    profile = await db.get(Profile, user_id)
    if not profile or not profile.status:
        raise SearchNotSetUp()
    task = await ledger.add(db, "match_user", user_id=user_id, dedupe=True)
    return TaskRef(task_id=task.id)
