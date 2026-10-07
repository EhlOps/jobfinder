import pytest
import sqlalchemy as sa
from factories import FakeAI, make_match, make_question, make_user, unknown
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.ai.claude_code import AIError
from jobfinder.ai.schemas import Consolidated, ConsolidatedQuestion
from jobfinder.config import get_settings
from jobfinder.models import ClarifyingQuestion, JobMatch, Profile, ProfileFact, Task
from jobfinder.questions import service


@pytest.fixture
def maker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def collected(db):
    return {m.id: m.questions_collected for m in await db.scalars(sa.select(JobMatch))}


async def test_only_unsure_promising_matches_are_considered(maker):
    async with maker() as db:
        u = await make_user(db)
        unsure, _ = await make_match(db, u.id, score=70, confidence=0.4, unknowns=[unknown("Have you used Kafka?")])
        confident, _ = await make_match(db, u.id, score=90, confidence=0.9, unknowns=[unknown("Ignored?")])
        poor, _ = await make_match(db, u.id, score=30, confidence=0.3, unknowns=[unknown("Also ignored?")])
        dismissed, _ = await make_match(db, u.id, score=70, confidence=0.4, status="dismissed", unknowns=[unknown("Dismissed?")])
        closed, _ = await make_match(db, u.id, score=70, confidence=0.4, active=False, unknowns=[unknown("Closed?")])
        ai = FakeAI()
        out = await service.collect_questions(db, ai, u.id)
        assert [r[0] for r in ai.calls[0]["raw"]] == ["Have you used Kafka?"]
        assert out["new_questions"] == 1 and out["reviewed"] == 1
        qs = (await db.scalars(sa.select(ClarifyingQuestion))).all()
        assert [(q.question, q.status, q.match_ids) for q in qs] == [("Have you used Kafka?", "pending", [unsure.id])]
        c = await collected(db)
        assert c[unsure.id] and c[confident.id] and c[poor.id]      # confident / poor ones are marked reviewed without an LLM call
        assert not c[dismissed.id] and not c[closed.id]             # these may become relevant again


async def test_no_llm_call_when_nothing_to_review(maker):
    async with maker() as db:
        u = await make_user(db)
        await make_match(db, u.id, score=90, confidence=0.9, unknowns=[unknown("x?")])
        ai = FakeAI()
        out = await service.collect_questions(db, ai, u.id)
        assert ai.calls == [] and out["new_questions"] == 0
        assert (await service.collect_questions(db, ai, u.id))["new_questions"] == 0      # and nothing is re-reviewed


async def test_consolidation_merges_sources_and_drops_answered(maker):
    async with maker() as db:
        u = await make_user(db)
        a, _ = await make_match(db, u.id, score=75, confidence=0.5, unknowns=[unknown("Do you use Copilot?"), unknown("Do you know Rust?")])
        b, _ = await make_match(db, u.id, score=72, confidence=0.5, unknowns=[unknown("Have you used AI coding tools?")])
        db.add(ProfileFact(user_id=u.id, question="Rust?", answer="Yes, two years", source="followup"))
        await db.commit()
        # raw order: 0 Copilot (a), 1 Rust (a), 2 AI tools (b)
        result = Consolidated(
            questions=[ConsolidatedQuestion(question="Have you used AI coding tools like Copilot?", why="many jobs ask", sources=[0, 2])],
            already_answered=[1],
        )
        ai = FakeAI(result)
        await service.collect_questions(db, ai, u.id)
        assert ai.calls[0]["facts"] == [("Rust?", "Yes, two years")]
        (q,) = (await db.scalars(sa.select(ClarifyingQuestion))).all()
        assert q.question == "Have you used AI coding tools like Copilot?" and sorted(q.match_ids) == sorted([a.id, b.id])
        assert all((await collected(db)).values())


async def test_duplicates_across_runs_and_existing_questions_are_not_recreated(maker):
    async with maker() as db:
        u = await make_user(db)
        await make_question(db, u.id, "Have you used Kafka?", status="skipped")
        await make_match(db, u.id, score=75, confidence=0.5, unknowns=[unknown("have you used kafka")])   # same text, different punctuation
        out = await service.collect_questions(db, FakeAI(), u.id)
        assert out["new_questions"] == 0 and len((await db.scalars(sa.select(ClarifyingQuestion))).all()) == 1
        # previously asked questions are passed to the model so it can drop paraphrases
        await make_match(db, u.id, score=75, confidence=0.5, unknowns=[unknown("Another new thing?")])
        ai = FakeAI()
        await service.collect_questions(db, ai, u.id)
        assert ai.calls[0]["asked"] == [("Have you used Kafka?", "skipped")]


async def test_open_question_cap_stops_collection(maker, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_open_questions", 2)
    async with maker() as db:
        u = await make_user(db)
        await make_question(db, u.id, "Q1?"), await make_question(db, u.id, "Q2?", status="emailed")
        m, _ = await make_match(db, u.id, score=75, confidence=0.5, unknowns=[unknown("Q3?")])
        ai = FakeAI()
        out = await service.collect_questions(db, ai, u.id)
        assert ai.calls == [] and "too many" in out["skipped"] and not (await collected(db))[m.id]


async def test_room_limits_how_many_are_created(maker, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_open_questions", 3)
    async with maker() as db:
        u = await make_user(db)
        await make_question(db, u.id, "Existing?")
        await make_match(db, u.id, score=75, confidence=0.5, unknowns=[unknown(f"New question {i}?") for i in range(5)])
        ai = FakeAI()
        await service.collect_questions(db, ai, u.id)
        assert ai.calls[0]["max"] == 2 and len((await db.scalars(sa.select(ClarifyingQuestion))).all()) == 3


async def test_ai_failure_leaves_matches_for_the_next_attempt(maker):
    async with maker() as db:
        u = await make_user(db)
        m, _ = await make_match(db, u.id, score=75, confidence=0.5, unknowns=[unknown("Kafka?")])
        with pytest.raises(AIError):
            await service.collect_questions(db, FakeAI(error=AIError("rate_limited", "limit")), u.id)
        assert not (await collected(db))[m.id] and (await db.scalars(sa.select(ClarifyingQuestion))).all() == []
        assert (await service.collect_questions(db, FakeAI(), u.id))["new_questions"] == 1


async def test_answering_creates_facts_bumps_version_and_queues_rescoring(maker):
    async with maker() as db:
        u = await make_user(db, version=3)
        q1, q2, q3 = [await make_question(db, u.id, f"Question {i}?", status=s) for i, s in enumerate(("pending", "emailed", "pending"))]
        out = await service.answer_questions(db, u.id, [
            {"id": q1.id, "answer": "  Yes, at Stripe  "}, {"id": q2.id, "skip": True}, {"id": q3.id, "answer": ""},   # empty, not skipped
        ])
        assert out == {"answered": 1, "skipped": 1, "remaining": 1}
        await db.refresh(q1), await db.refresh(q2), await db.refresh(q3)
        assert (q1.status, q1.answer) == ("answered", "Yes, at Stripe") and q2.status == "skipped" and q3.status == "pending"
        fact = (await db.scalars(sa.select(ProfileFact))).one()
        assert (fact.question, fact.answer, fact.source) == ("Question 0?", "Yes, at Stripe", "job_question")
        assert (await db.get(Profile, u.id)).version == 4
        tasks = (await db.scalars(sa.select(Task).where(Task.kind == "match_user"))).all()
        assert [t.user_id for t in tasks] == [u.id]


async def test_skipping_only_does_not_touch_profile_or_queue_scoring(maker):
    async with maker() as db:
        u = await make_user(db, version=3)
        q = await make_question(db, u.id)
        await service.answer_questions(db, u.id, [{"id": q.id, "skip": True}])
        assert (await db.get(Profile, u.id)).version == 3 and (await db.scalars(sa.select(Task))).all() == []


async def test_cannot_answer_other_users_or_already_answered_questions(maker):
    async with maker() as db:
        a, b = await make_user(db, "a@x.com"), await make_user(db, "b@x.com")
        theirs = await make_question(db, b.id)
        done = await make_question(db, a.id, status="answered")
        out = await service.answer_questions(db, a.id, [{"id": theirs.id, "answer": "hijack"}, {"id": done.id, "answer": "again"}, {"id": 9999, "answer": "x"}])
        assert out["answered"] == 0 and (await db.scalars(sa.select(ProfileFact))).all() == []
        await db.refresh(theirs)
        assert theirs.status == "pending" and theirs.answer is None


async def test_long_answers_are_truncated(maker):
    async with maker() as db:
        u = await make_user(db)
        q = await make_question(db, u.id)
        await service.answer_questions(db, u.id, [{"id": q.id, "answer": "x" * 5000}])
        assert len((await db.scalars(sa.select(ProfileFact.answer))).one()) == service.MAX_ANSWER_CHARS


async def test_list_open_includes_related_jobs_only_for_the_owner(maker):
    async with maker() as db:
        u, other = await make_user(db, "a@x.com"), await make_user(db, "b@x.com")
        m, job = await make_match(db, u.id, company="Acme")
        om, _ = await make_match(db, other.id, company="Secret Corp")
        await make_question(db, u.id, "Kafka?", match_ids=[m.id, om.id])
        await make_question(db, u.id, "Answered one", status="answered")
        (item,) = await service.list_open(db, u.id)
        assert item["question"] == "Kafka?" and item["jobs"] == [{"match_id": m.id, "title": job.title, "company": "Acme"}]
