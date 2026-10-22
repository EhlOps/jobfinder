from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from jobfinder.core.config import get_settings
from jobfinder.integrations.claude.claude_code import AIError
from jobfinder.integrations.claude.schemas import MatchScore
from jobfinder.models import Company, Job, JobMatch, Profile, ProfileFact, Task, User
from jobfinder.models.posting import dedupe_hash
from jobfinder.repositories import matches as matches_repo
from jobfinder.services.matching import service
from jobfinder.tasks import ledger
from tests.factories import make_match
from tests.fakes import FakeScoringAI, run_task

STATUS = {"target_roles": ["Backend Engineer"], "target_locations": ["Boston"], "is_new_grad": True,
          "salary_min": 100000, "needs_visa_sponsorship": False}


@pytest.fixture(autouse=True)
def budget(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "match_daily_llm_budget", 25)
    monkeypatch.setattr(s, "ai_max_concurrency", 2)
    monkeypatch.setattr(s, "match_slice_jobs", 25)


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
        ai = FakeScoringAI()
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
        ai = FakeScoringAI()
        await service.match_user(db, ai, u.id)
        assert ai.calls == ["Backend Engineer"]
        # a senior candidate sees them
        u2 = await make_user(db, {**STATUS, "is_new_grad": False, "seniority": ["senior"]}, email="s@x.com")
        ai2 = FakeScoringAI()
        await service.match_user(db, ai2, u2.id)
        assert "Backend Engineer B" in ai2.calls


async def test_visa_filter_only_applies_when_candidate_needs_sponsorship(maker):
    async with maker() as db:
        u = await make_user(db, {**STATUS, "needs_visa_sponsorship": True})
        await make_job(db, "Backend Engineer", description_text="No visa sponsorship available.")
        await make_job(db, "Backend Engineer B", description_text="We sponsor visas.")
        ai = FakeScoringAI()
        await service.match_user(db, ai, u.id)
        assert ai.calls == ["Backend Engineer B"]


async def test_budget_limits_scoring_and_reports_pending(maker):
    async with maker() as db:
        u = await make_user(db)
        for i in range(5):
            await make_job(db, f"Backend Engineer {i}")
        ai = FakeScoringAI()
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


async def test_per_user_budget_overrides_default_and_can_be_turned_off(maker):
    async with maker() as db:
        u = await make_user(db)
        for i in range(4):
            await make_job(db, f"Backend Engineer {i}")
        u.match_budget = 2
        await db.commit()
        assert await service.remaining_budget(db, u.id) == 2
        ai = FakeScoringAI()
        out = await service.match_user(db, ai, u.id)
        assert out["scored"] == 2 and out["pending"] == 2
        u.match_budget_enabled = False
        await db.commit()
        assert await service.remaining_budget(db, u.id) == service.UNLIMITED
        out = await service.match_user(db, ai, u.id)
        assert out["scored"] == 2 and out["pending"] == 0 and len(await matches(db)) == 4


async def test_best_prefilter_candidates_are_scored_first(maker):
    async with maker() as db:
        skills = [{"name": s} for s in ("Python", "Go", "Postgres", "Kafka")]
        u = await make_user(db, STATUS, {"skills": skills})
        await make_job(db, "Data Analyst", description_text="spreadsheets")
        await make_job(db, "Backend Engineer, Payments", description_text="spreadsheets")
        await make_job(db, "Backend Engineer", description_text="python go postgres kafka")
        await make_job(db, "Backend Engineer, Infra", description_text="python only")
        ai = FakeScoringAI()
        await service.match_user(db, ai, u.id, budget=2)
        assert ai.calls == ["Backend Engineer", "Backend Engineer, Infra"]   # most skill overlap first


async def test_already_matched_and_duplicate_postings_are_not_rescored(maker):
    async with maker() as db:
        u = await make_user(db)
        h = dedupe_hash("Acme", "Backend Engineer", "Boston, MA")
        await make_job(db, "Backend Engineer", dedupe_hash=h)
        await make_job(db, "Backend Engineer", dedupe_hash=h)       # same posting on another requisition
        ai = FakeScoringAI()
        await service.match_user(db, ai, u.id)
        assert len(ai.calls) == 1
        await make_job(db, "Backend Engineer", dedupe_hash=h)       # a third copy shows up later
        await service.match_user(db, ai, u.id)
        assert len(ai.calls) == 1                                    # matched hash is remembered


async def test_profile_change_rescoring_keeps_status_and_skips_dismissed(maker):
    async with maker() as db:
        u = await make_user(db, version=1)
        saved, new_good, dismissed, new_poor = [await make_job(db, t) for t in ("Backend Engineer S", "Backend Engineer N", "Backend Engineer D", "Backend Engineer P")]
        ai = FakeScoringAI({"Backend Engineer P": 30})
        await service.match_user(db, ai, u.id)
        statuses = {saved.id: "saved", dismissed.id: "dismissed"}
        for jid, st in statuses.items():
            await db.execute(sa.update(JobMatch).where(JobMatch.job_id == jid).values(status=st))
        await db.commit()

        (await db.get(Profile, u.id)).version = 2
        await db.commit()
        ai2 = FakeScoringAI({"Backend Engineer S": 95})
        out = await service.match_user(db, ai2, u.id)
        # saved first, then by prefilter score and newest posting (P is newer than N), whatever the old LLM score:
        # low scores are refreshed too (answers often fill the gap); dismissed is left alone
        assert ai2.calls == ["Backend Engineer S", "Backend Engineer P", "Backend Engineer N"] and out["scored"] == 3
        rows = {m.job_id: m for m in (await db.scalars(sa.select(JobMatch)))}
        assert rows[saved.id].llm_score == 95 and rows[saved.id].status == "saved" and rows[saved.id].profile_version == 2
        assert rows[dismissed.id].profile_version == 1 and rows[new_poor.id].profile_version == 2
        assert rows[new_good.id].profile_version == 2


def _c(job_id, score, posted, match_id=None, saved=False):
    return service.Candidate(job_id, score, posted, f"h{job_id}", match_id=match_id, saved=saved)


def test_rank_work_puts_answered_then_saved_first_then_interleaves_by_score_and_posting():
    stale = [_c(1, 10, 5, match_id=101), _c(2, 90, 5, match_id=102), _c(3, 5, 1, match_id=103, saved=True),
             _c(4, 50, 9, match_id=104), _c(5, 1, 1, match_id=105, saved=True)]
    new = [_c(6, 70, 1), _c(7, 50, 10), _c(8, 95, 0)]
    order = [w.job_id for w in service.rank_work(stale, new, answered={105, 101})]
    # answered (by score), saved, then stale and new together by score, ties broken by the newer posting
    assert order == [1, 5, 3, 8, 2, 6, 7, 4]
    assert [w.job_id for w in service.rank_work([], new, set())] == [8, 6, 7]


async def test_stale_rows_are_light_and_skip_dismissed_and_current(maker):
    async with maker() as db:
        u = await make_user(db, version=2)
        (a, _), (b, _), (c, _) = [await make_match(db, u.id, score=90 - i) for i in range(3)]
        b.status, c.profile_version = "dismissed", 2
        await db.commit()
        rows = await matches_repo.stale(db, u.id, 2, service.STALE_STATUSES)
        assert [(r.match_id, r.status) for r in rows] == [(a.id, a.status)] and not hasattr(rows[0], "description_text")


async def test_rate_limit_mid_run_keeps_progress_and_reraises(maker):
    async with maker() as db:
        u = await make_user(db)
        for t in ("Backend Engineer A", "Backend Engineer B", "Backend Engineer C", "Backend Engineer D"):
            await make_job(db, t)  # equal scores: the newest job (D) is scored first, so A and B are the second batch
        ai = FakeScoringAI(errors={"Backend Engineer A": AIError("rate_limited", "usage limit"), "Backend Engineer B": AIError("rate_limited", "usage limit")})
        with pytest.raises(AIError) as e:
            await service.match_user(db, ai, u.id, checkpoint=db.commit)  # the handler's checkpoint
        assert e.value.retryable
        await db.rollback()                                          # what the worker does with the AIError
        assert len(await matches(db)) == 2                           # first batch survived
        assert (await db.get(Profile, u.id)).match_summary["scored"] == 2
        # the retry continues where it left off
        ai2 = FakeScoringAI()
        out = await service.match_user(db, ai2, u.id)
        assert out["scored"] == 2 and len(await matches(db)) == 4


async def test_unparseable_answer_is_skipped_not_fatal(maker):
    async with maker() as db:
        u = await make_user(db)
        for t in ("Backend Engineer A", "Backend Engineer B"):
            await make_job(db, t)
        ai = FakeScoringAI(errors={"Backend Engineer A": AIError("bad_output", "junk")})
        out = await service.match_user(db, ai, u.id)
        assert out["scored"] == 1 and out["failed"] == 1 and set(await matches(db)) == {"Backend Engineer B"}
        assert out["pending"] == 0  # left for the next run: a slice doesn't chain just to retry it


async def test_no_profile_or_empty_profile_does_nothing(maker):
    async with maker() as db:
        u = await make_user(db, status={})
        await make_job(db)
        ai = FakeScoringAI()
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

        class SpyAI(FakeScoringAI):
            async def score_match(self, status, background, facts, job, dossier=None, stage_info=None):
                seen.update(job)
                return await super().score_match(status, background, facts, job)

        await service.match_user(db, SpyAI(), u.id)
        assert seen["company_size"] == "large" and seen["company_industry"] == "fintech" and seen["title"] == "Backend Engineer"


async def test_rescoring_resets_questions_collected(maker):
    async with maker() as db:
        u = await make_user(db, version=1)
        job = await make_job(db, "Backend Engineer")
        await service.match_user(db, FakeScoringAI(), u.id)
        await db.execute(sa.update(JobMatch).values(questions_collected=True))
        await db.commit()
        (await db.get(Profile, u.id)).version = 2
        await db.commit()
        await service.match_user(db, FakeScoringAI(), u.id)
        m = (await db.scalars(sa.select(JobMatch).where(JobMatch.job_id == job.id))).one()
        assert m.profile_version == 2 and m.questions_collected is False       # new assessment -> look at its unknowns again


async def test_candidates_are_light_batched_and_hydrated_in_order(maker, monkeypatch):
    """Descriptions are loaded BATCH at a time and only the jobs that get scored are loaded in full."""
    svc = service
    monkeypatch.setattr(svc, "BATCH", 2)
    monkeypatch.setattr(get_settings(), "match_min_prefilter", 0.0)
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


async def test_requirement_checks_drive_the_stored_score_and_persist(maker):
    from jobfinder.integrations.claude.schemas import RequirementCheck

    class ReqAI(FakeScoringAI):
        async def score_match(self, status, background, facts, job, dossier=None, stage_info=None):
            return MatchScore(
                score=99, confidence=0.99, hire_verdict="maybe", recruiter_take="Promising; Kafka unconfirmed.",
                requirements=[
                    RequirementCheck(requirement="Python", status="met", evidence="3 yrs"),
                    RequirementCheck(requirement="Kafka", status="unknown", question="Have you run Kafka?"),
                ],
            )

    async with maker() as db:
        u = await make_user(db)
        await make_job(db)
        await service.match_user(db, ReqAI(), u.id)
        m = (await db.scalars(sa.select(JobMatch))).one()
        assert m.llm_score == 74 and m.confidence == 0.5  # 0.8*75 + 0.2*70 (both must-haves; Kafka unknown); the model's 99 is ignored
        assert [r["requirement"] for r in m.requirements] == ["Python", "Kafka"]
        assert m.unknowns[0]["question"] == "Have you run Kafka?" and m.hire_verdict == "maybe"


# ── students and new grads ───────────────────────────────────────────────
def _status(**kw):
    return {**STATUS, "is_new_grad": None, **kw}


async def test_student_sees_internships_not_new_grad_roles_until_final_year(maker):
    from jobfinder.services.matching import prefilter

    today = datetime.now(UTC).date()
    far = f"{today.year + 2}-{today.month:02d}"
    soon = f"{today.year}-{today.month:02d}"
    async with maker() as db:
        u = await make_user(db, status=_status(graduation_date=far))
        await make_job(db, "Backend Engineer Intern", seniority="intern")
        await make_job(db, "Backend Engineer, New Grad", seniority="new_grad")
        await service.match_user(db, FakeScoringAI(), u.id)
        assert set(await matches(db)) == {"Backend Engineer Intern"}
        (await db.get(Profile, u.id)).status = _status(graduation_date=soon)
        await db.commit()
        await service.match_user(db, FakeScoringAI(), u.id)
        assert set(await matches(db)) == {"Backend Engineer Intern", "Backend Engineer, New Grad"}
    assert prefilter.career_stage(_status(graduation_date=soon)) == "final_year"


async def test_postings_with_other_graduation_windows_are_skipped(maker):
    async with maker() as db:
        u = await make_user(db, status=_status(graduation_date="2027-05"))
        await make_job(db, "Backend Engineer, New Grad A", seniority="new_grad", description_text="Python. Open to graduating between Dec 2026 and Jun 2027.")
        await make_job(db, "Backend Engineer, New Grad B", seniority="new_grad", description_text="Python. Class of 2029 only.")
        await make_job(db, "Backend Engineer, New Grad C", seniority="new_grad", description_text="Python Postgres.")
        await service.match_user(db, FakeScoringAI(), u.id)
        assert set(await matches(db)) == {"Backend Engineer, New Grad A", "Backend Engineer, New Grad C"}


async def test_early_career_roles_rank_first_for_a_graduating_student(maker):
    async with maker() as db:
        u = await make_user(db, status=_status(graduation_date="2027-05", seniority=["new_grad", "mid"]))
        await make_job(db, "Backend Engineer", seniority="mid", description_text="Python Postgres")
        await make_job(db, "Backend Engineer, New Grad", seniority="new_grad", description_text="Python Postgres")
        ai = FakeScoringAI()
        await service.match_user(db, ai, u.id, budget=1)
        assert ai.calls == ["Backend Engineer, New Grad"]


async def test_scorer_is_told_the_career_stage_only_for_students(maker):
    seen = {}

    class SpyAI(FakeScoringAI):
        async def score_match(self, status, background, facts, job, dossier=None, stage_info=None):
            seen[job["title"]] = stage_info
            return await super().score_match(status, background, facts, job)

    async with maker() as db:
        u = await make_user(db, status=_status(graduation_date="2027-05", seniority=["new_grad", "mid"]), email="s@x.com")
        v = await make_user(db, status=_status(seniority=["mid"]), email="v@x.com")
        await make_job(db, "Backend Engineer", seniority="mid")
        await service.match_user(db, SpyAI(), u.id)
        assert seen["Backend Engineer"]["stage"] in ("student", "final_year", "recent_grad")
        assert seen["Backend Engineer"]["graduation"] == "2027-05"
        seen.clear()
        await service.match_user(db, SpyAI(), v.id)
        assert seen["Backend Engineer"] is None


async def test_stage_change_bumps_the_version_but_the_first_record_does_not(maker):
    from datetime import date

    async with maker() as db:
        u = await make_user(db, status=_status(graduation_date="2027-05"), version=3)
        p = await db.get(Profile, u.id)
        assert service.sync_career_stage(p, date(2026, 1, 15)) is False and p.career_stage == "student" and p.version == 3
        assert service.sync_career_stage(p, date(2026, 2, 1)) is False and p.version == 3    # same stage: nothing
        assert service.sync_career_stage(p, date(2026, 6, 1)) is True                        # 11 months out: final year
        assert (p.career_stage, p.version) == ("final_year", 4)
        assert service.sync_career_stage(p, date(2027, 8, 1)) is True                        # graduated
        assert (p.career_stage, p.version) == ("recent_grad", 5)
        assert service.sync_career_stage(p, date(2027, 9, 1)) is False


async def test_new_user_defaults_match_budget_enabled_true(maker):
    # `cli add-user` inserts User(email=...) only; the column is NOT NULL (migration 0012)
    async with maker() as db:
        db.add(User(email="new@example.com"))
        await db.commit()
    async with maker() as db:
        u = (await db.scalars(sa.select(User).where(User.email == "new@example.com"))).one()
        assert u.match_budget_enabled is True


# ── slices that chain themselves (#124) ──────────────────────────────────
async def test_a_slice_scores_at_most_limit_and_reports_whats_left(maker):
    async with maker() as db:
        u = await make_user(db)
        for i in range(7):
            await make_job(db, f"Backend Engineer {i}")
        ai = FakeScoringAI()
        out = await service.match_user(db, ai, u.id, limit=3)
        assert len(ai.calls) == 3 and (out["scored"], out["pending"], out["budget_left"], out["failed"]) == (3, 4, 22, 0)
        out = await service.match_user(db, ai, u.id, budget=2, limit=3)   # the budget is the tighter bound
        assert (out["scored"], out["pending"], out["budget_left"]) == (2, 2, 0)
        assert (await db.get(Profile, u.id)).match_summary == out


async def _chain(maker, ai, user_id):
    """Run match_user through the runtime and follow its queued slices until the chain ends."""
    async with maker() as db:
        task = await ledger.add(db, "match_user", user_id=user_id)
        await db.commit()
    results, tid = [], task.id
    while tid is not None:
        await run_task(tid, ai, maker)
        async with maker() as db:
            results.append((await db.get(Task, tid)).result)
            tid = await db.scalar(sa.select(Task.id).where(Task.kind == "match_user", Task.status == "queued"))
    async with maker() as db:
        questions = (await db.scalars(sa.select(Task).where(Task.kind == "collect_questions"))).all()
    return results, questions


async def test_the_chain_runs_slices_in_relevance_order_and_collects_questions_once(maker, monkeypatch):
    monkeypatch.setattr(get_settings(), "match_slice_jobs", 4)
    async with maker() as db:
        u = await make_user(db, version=2)
        u.match_budget_enabled = False
        old = datetime.now(UTC) - timedelta(days=3)
        for i in range(6):  # new candidates, newest last
            await make_job(db, f"Backend Engineer N{i}", posted_at=old + timedelta(hours=i))
        await make_match(db, u.id, title="Backend Engineer Saved", status="saved")
        _, ans_job = await make_match(db, u.id, title="Backend Engineer Answered")
        db.add(ProfileFact(user_id=u.id, question="q", answer="a", source="job_question", job_id=ans_job.id))
        await db.commit()
    ai = FakeScoringAI()
    results, questions = await _chain(maker, ai, u.id)
    assert [r["scored"] for r in results] == [4, 4]
    assert [r["pending"] for r in results] == [4, 0] and "next_slice" not in results[1]
    assert isinstance(results[0]["next_slice"], int)  # the next slice's task id, for the matches page
    assert ai.calls[:2] == ["Backend Engineer Answered", "Backend Engineer Saved"]
    assert ai.calls[2:] == [f"Backend Engineer N{i}" for i in (5, 4, 3, 2, 1, 0)]  # equal scores: newest first
    assert len(questions) == 1


async def test_the_chain_stops_when_the_budget_runs_out(maker, monkeypatch):
    monkeypatch.setattr(get_settings(), "match_slice_jobs", 2)
    async with maker() as db:
        u = await make_user(db)
        u.match_budget = 3
        for i in range(6):
            await make_job(db, f"Backend Engineer {i}")
        await db.commit()
    results, questions = await _chain(maker, FakeScoringAI(), u.id)
    assert [(r["scored"], r["budget_left"], r["pending"]) for r in results] == [(2, 1, 4), (1, 0, 3)]
    assert len(questions) == 1
    # with no budget left, the slice scores nothing: the chain neither continues nor collects again
    results, questions = await _chain(maker, FakeScoringAI(), u.id)
    assert [(r["scored"], r["budget_left"]) for r in results] == [(0, 0)] and len(questions) == 1


async def test_nothing_to_score_ends_the_chain_without_collecting(maker):
    async with maker() as db:
        u = await make_user(db)
    results, questions = await _chain(maker, FakeScoringAI(), u.id)
    assert [(r["scored"], r["pending"]) for r in results] == [(0, 0)] and questions == []


async def test_a_rate_limit_mid_slice_keeps_what_was_scored_and_queues_nothing(maker):
    async with maker() as db:
        u = await make_user(db)
        for t in ("Backend Engineer A", "Backend Engineer B", "Backend Engineer C", "Backend Engineer D"):
            await make_job(db, t)  # equal scores, newest first: D and C, then B and A
        task = await ledger.add(db, "match_user", user_id=u.id)
        await db.commit()
    ai = FakeScoringAI(errors={"Backend Engineer A": AIError("rate_limited", "usage limit")})
    await run_task(task.id, ai, maker)
    async with maker() as db:
        assert set(await matches(db)) == {"Backend Engineer D", "Backend Engineer C", "Backend Engineer B"}
        assert (await db.get(Profile, u.id)).match_summary["pending"] == 1
        rows = [(t.kind, t.status) for t in await db.scalars(sa.select(Task).order_by(Task.id))]
    assert rows == [("match_user", "queued")]  # the same task, put back for a retry; no new slice, no questions


async def test_a_chain_whose_last_slice_scores_nothing_still_collects_questions(maker, monkeypatch):
    monkeypatch.setattr(get_settings(), "match_slice_jobs", 2)
    async with maker() as db:
        u = await make_user(db)
        old = datetime.now(UTC) - timedelta(days=1)
        await make_job(db, "Backend Engineer Junk", posted_at=old)  # oldest: ranked last, alone in slice 2
        for t in ("Backend Engineer A", "Backend Engineer B"):
            await make_job(db, t)
    ai = FakeScoringAI(errors={"Backend Engineer Junk": AIError("bad_output", "junk")})
    results, questions = await _chain(maker, ai, u.id)
    assert [(r["scored"], r["failed"], r["pending"]) for r in results] == [(2, 0, 1), (0, 1, 0)]
    assert len(questions) == 1
