from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.ai.claude_code import AIError
from jobfinder.ai.schemas import MatchScore, Unknown
from jobfinder.config import get_settings
from jobfinder.ingest.base import dedupe_hash
from jobfinder.matching import service
from jobfinder.models import Company, Job, JobMatch, Profile, Task, User

STATUS = {"target_roles": ["Backend Engineer"], "target_locations": ["Boston"], "is_new_grad": True,
          "salary_min": 100000, "needs_visa_sponsorship": False}


class FakeAI:
    """score_match returns a canned score by job title; titles can be told to fail."""

    def __init__(self, scores=None, errors=None):
        self.scores, self.errors, self.calls = scores or {}, errors or {}, []

    async def score_match(self, status, background, facts, job):
        self.calls.append(job["title"])
        if (err := self.errors.get(job["title"])) is not None:
            raise err
        return MatchScore(
            score=self.scores.get(job["title"], 80), confidence=0.9, reasons=["fits"], gaps=["k8s"],
            unknowns=[Unknown(question="Used Kafka?", why="job needs it")],
        )


@pytest.fixture
def maker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def budget(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "match_daily_llm_budget", 25)
    monkeypatch.setattr(s, "ai_max_concurrency", 2)


async def make_user(db, status=None, background=None, email="u@x.com", version=3):
    u = User(email=email, password_hash="x")
    db.add(u)
    await db.flush()
    db.add(Profile(user_id=u.id, status=STATUS if status is None else status, background=background or {}, version=version))
    await db.commit()
    return u


_n = 0


async def make_job(db, title="Backend Engineer", location="Boston, MA", **kw):
    global _n
    _n += 1
    j = Job(
        source="greenhouse", external_id=f"e{_n}", company_name=kw.pop("company_name", "Acme"), title=title,
        url=f"https://x/{_n}", location=location, description_text=kw.pop("description_text", "Python Postgres services"),
        dedupe_hash=kw.pop("dedupe_hash", dedupe_hash("Acme", f"{title}{_n}", location)),
        posted_at=kw.pop("posted_at", datetime.now(UTC)), **kw,
    )
    db.add(j)
    await db.commit()
    return j


async def matches(db):
    return {j: m for m, j in (await db.execute(sa.select(JobMatch, Job.title).join(Job, Job.id == JobMatch.job_id))).all()}


async def test_scores_candidates_and_applies_hard_filters(maker):
    async with maker() as db:
        u = await make_user(db)
        ok = await make_job(db, "Backend Engineer", seniority="junior")
        await make_job(db, "Senior Backend Engineer", seniority="senior")                      # level
        await make_job(db, "Backend Engineer II", location="Dublin, Ireland")                   # place
        await make_job(db, "Backend Engineer III", salary_max=80000, salary_currency="USD")      # pay below floor
        await make_job(db, "Backend Engineer IV", description_text="Unable to provide visa sponsorship.")  # fine: user needs none
        await make_job(db, "Backend Engineer V", is_active=False)                                # closed
        ai = FakeAI()
        out = await service.match_user(db, ai, u.id)
        assert sorted(ai.calls) == ["Backend Engineer", "Backend Engineer IV"]
        got = await matches(db)
        assert set(got) == {"Backend Engineer", "Backend Engineer IV"}
        m = got["Backend Engineer"]
        assert (m.llm_score, m.verdict, m.status, m.profile_version) == (80, "strong", "new", 3)
        assert m.reasons == ["fits"] and m.gaps == ["k8s"] and m.unknowns == [{"question": "Used Kafka?", "why": "job needs it"}]
        assert m.job_id == ok.id and out["scored"] == 2 and out["pending"] == 0
        assert (await db.get(Profile, u.id)).match_summary["scored"] == 2


async def test_entry_level_candidate_is_not_scored_against_experienced_roles(maker):
    async with maker() as db:
        u = await make_user(db)
        await make_job(db, "Backend Engineer", description_text="Great intro role. 1+ years of experience helpful.")
        await make_job(db, "Backend Engineer B", description_text="Requires 5+ years of experience building services.")
        await make_job(db, "Backend Engineer C", description_text="3-5 years of professional engineering experience")
        ai = FakeAI()
        await service.match_user(db, ai, u.id)
        assert ai.calls == ["Backend Engineer"]
        # a senior candidate sees them
        u2 = await make_user(db, {**STATUS, "is_new_grad": False, "seniority": ["senior"]}, email="s@x.com")
        ai2 = FakeAI()
        await service.match_user(db, ai2, u2.id)
        assert "Backend Engineer B" in ai2.calls


async def test_visa_filter_only_applies_when_candidate_needs_sponsorship(maker):
    async with maker() as db:
        u = await make_user(db, {**STATUS, "needs_visa_sponsorship": True})
        await make_job(db, "Backend Engineer", description_text="No visa sponsorship available.")
        await make_job(db, "Backend Engineer B", description_text="We sponsor visas.")
        ai = FakeAI()
        await service.match_user(db, ai, u.id)
        assert ai.calls == ["Backend Engineer B"]


async def test_budget_limits_scoring_and_reports_pending(maker):
    async with maker() as db:
        u = await make_user(db)
        for i in range(5):
            await make_job(db, f"Backend Engineer {i}")
        ai = FakeAI()
        out = await service.match_user(db, ai, u.id, budget=2)
        assert len(ai.calls) == 2 and out["scored"] == 2 and out["pending"] == 3
        # the rolling-24h budget counts what was already scored
        assert await service.remaining_budget(db, u.id) == 23
        get_settings().match_daily_llm_budget = 3
        assert await service.remaining_budget(db, u.id) == 1
        await service.match_user(db, ai, u.id)
        assert len(await matches(db)) == 3
        # old scores fall out of the window
        await db.execute(sa.update(JobMatch).values(scored_at=datetime.now(UTC) - timedelta(hours=30)))
        await db.commit()
        assert await service.remaining_budget(db, u.id) == 3


async def test_best_prefilter_candidates_are_scored_first(maker):
    async with maker() as db:
        skills = [{"name": s} for s in ("Python", "Go", "Postgres", "Kafka")]
        u = await make_user(db, STATUS, {"skills": skills})
        await make_job(db, "Data Analyst", description_text="spreadsheets")
        await make_job(db, "Backend Engineer, Payments", description_text="spreadsheets")
        await make_job(db, "Backend Engineer", description_text="python go postgres kafka")
        await make_job(db, "Backend Engineer, Infra", description_text="python only")
        ai = FakeAI()
        await service.match_user(db, ai, u.id, budget=2)
        assert ai.calls == ["Backend Engineer", "Backend Engineer, Infra"]   # most skill overlap first


async def test_already_matched_and_duplicate_postings_are_not_rescored(maker):
    async with maker() as db:
        u = await make_user(db)
        h = dedupe_hash("Acme", "Backend Engineer", "Boston, MA")
        await make_job(db, "Backend Engineer", dedupe_hash=h)
        await make_job(db, "Backend Engineer", dedupe_hash=h)       # same posting on another requisition
        ai = FakeAI()
        await service.match_user(db, ai, u.id)
        assert len(ai.calls) == 1
        await make_job(db, "Backend Engineer", dedupe_hash=h)       # a third copy shows up later
        await service.match_user(db, ai, u.id)
        assert len(ai.calls) == 1                                    # matched hash is remembered


async def test_profile_change_rescoring_keeps_status_and_skips_dismissed(maker):
    async with maker() as db:
        u = await make_user(db, version=1)
        saved, new_good, dismissed, new_poor = [await make_job(db, t) for t in ("Backend Engineer S", "Backend Engineer N", "Backend Engineer D", "Backend Engineer P")]
        ai = FakeAI({"Backend Engineer P": 30})
        await service.match_user(db, ai, u.id)
        statuses = {saved.id: "saved", dismissed.id: "dismissed"}
        for jid, st in statuses.items():
            await db.execute(sa.update(JobMatch).where(JobMatch.job_id == jid).values(status=st))
        await db.commit()

        (await db.get(Profile, u.id)).version = 2
        await db.commit()
        ai2 = FakeAI({"Backend Engineer S": 95})
        out = await service.match_user(db, ai2, u.id)
        # saved + promising new are refreshed (saved first); dismissed and the poor match are left alone
        assert ai2.calls == ["Backend Engineer S", "Backend Engineer N"] and out["scored"] == 2
        rows = {m.job_id: m for m in (await db.scalars(sa.select(JobMatch)))}
        assert rows[saved.id].llm_score == 95 and rows[saved.id].status == "saved" and rows[saved.id].profile_version == 2
        assert rows[dismissed.id].profile_version == 1 and rows[new_poor.id].profile_version == 1
        assert rows[new_good.id].profile_version == 2


async def test_rate_limit_mid_run_keeps_progress_and_reraises(maker):
    async with maker() as db:
        u = await make_user(db)
        for t in ("Backend Engineer A", "Backend Engineer B", "Backend Engineer C", "Backend Engineer D"):
            await make_job(db, t)  # equal scores: the newest job (D) is scored first, so A and B are the second batch
        ai = FakeAI(errors={"Backend Engineer A": AIError("rate_limited", "usage limit"), "Backend Engineer B": AIError("rate_limited", "usage limit")})
        with pytest.raises(AIError) as e:
            await service.match_user(db, ai, u.id)
        assert e.value.retryable
        assert len(await matches(db)) == 2                           # first batch survived
        assert (await db.get(Profile, u.id)).match_summary["scored"] == 2
        # the retry continues where it left off
        ai2 = FakeAI()
        out = await service.match_user(db, ai2, u.id)
        assert out["scored"] == 2 and len(await matches(db)) == 4


async def test_unparseable_answer_is_skipped_not_fatal(maker):
    async with maker() as db:
        u = await make_user(db)
        for t in ("Backend Engineer A", "Backend Engineer B"):
            await make_job(db, t)
        ai = FakeAI(errors={"Backend Engineer A": AIError("bad_output", "junk")})
        out = await service.match_user(db, ai, u.id)
        assert out["scored"] == 1 and out["failed"] == 1 and set(await matches(db)) == {"Backend Engineer B"}


async def test_no_profile_or_empty_profile_does_nothing(maker):
    async with maker() as db:
        u = await make_user(db, status={})
        await make_job(db)
        ai = FakeAI()
        out = await service.match_user(db, ai, u.id)
        assert ai.calls == [] and "skipped" in out
        assert (await service.match_user(db, ai, 9999))["scored"] == 0


async def test_company_data_feeds_prefilter_and_prompt(maker):
    async with maker() as db:
        u = await make_user(db, {**STATUS, "prestige_preference": 5, "company_sizes": ["large"]})
        c = Company(name="Acme", ats="greenhouse", slug="acme", prestige_tier=5, size="large", industry="fintech")
        db.add(c)
        await db.commit()
        await make_job(db, "Backend Engineer", company_id=c.id)

        seen = {}

        class SpyAI(FakeAI):
            async def score_match(self, status, background, facts, job):
                seen.update(job)
                return await super().score_match(status, background, facts, job)

        await service.match_user(db, SpyAI(), u.id)
        assert seen["company_size"] == "large" and seen["company_industry"] == "fintech" and seen["title"] == "Backend Engineer"


async def test_enqueue_for_active_users_skips_unfinished_profiles(maker):
    async with maker() as db:
        a = await make_user(db, email="a@x.com")
        await make_user(db, status={}, email="b@x.com")
        assert await service.enqueue_for_active_users(db) == 1
        assert await service.enqueue_for_active_users(db) == 1          # deduped while pending
        tasks = (await db.scalars(sa.select(Task).where(Task.kind == "match_user"))).all()
        assert [t.user_id for t in tasks] == [a.id]


async def test_rescoring_resets_questions_collected(maker):
    async with maker() as db:
        u = await make_user(db, version=1)
        job = await make_job(db, "Backend Engineer")
        await service.match_user(db, FakeAI(), u.id)
        await db.execute(sa.update(JobMatch).values(questions_collected=True))
        await db.commit()
        (await db.get(Profile, u.id)).version = 2
        await db.commit()
        await service.match_user(db, FakeAI(), u.id)
        m = (await db.scalars(sa.select(JobMatch).where(JobMatch.job_id == job.id))).one()
        assert m.profile_version == 2 and m.questions_collected is False       # new assessment -> look at its unknowns again


async def test_candidates_are_light_batched_and_hydrated_in_order(engine, monkeypatch):
    """Descriptions are loaded BATCH at a time and only the jobs that get scored are loaded in full."""
    svc = service
    monkeypatch.setattr(svc, "BATCH", 2)
    monkeypatch.setattr(get_settings(), "match_min_prefilter", 0.0)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        u = await make_user(db, status={"target_roles": ["Backend Engineer"]})
        for n in range(5):
            await make_job(db, f"Backend Engineer {n}")
        profile = await db.get(Profile, u.id)
        cands = await svc.find_candidates(db, u.id, profile)
        assert len(cands) == 5 and all(isinstance(c, svc.Candidate) for c in cands)
        assert not hasattr(cands[0], "job") and cands[0].score >= cands[-1].score
        work = await svc.hydrate(db, cands[:3])
        assert [w[0].id for w in work] == [c.job_id for c in cands[:3]] and all(w[3] is False for w in work)
        assert await svc.hydrate(db, []) == []
