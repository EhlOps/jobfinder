import sqlalchemy as sa
from factories import make_match, make_user

from jobfinder.models import JobMatch, Profile, ProfileFact, Task
from jobfinder.profile import interview
from tests.fakes import FakeAuditAI


async def test_audit_stores_dossier_questions_and_queues_matching(maker):
    async with maker() as db:
        u = await make_user(db)
        out = await interview.run_audit(db, FakeAuditAI(), u.id)
        p = await db.get(Profile, u.id)
        assert out["readiness"] == 62 and p.readiness == 62 and p.dossier["headline"] == "Backend engineer"
        assert [q["question"] for q in p.audit["questions"]] == ["What did you own at Acme?", "Notice period?"]
        assert (await db.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.kind == "match_user"))) == 1


async def test_demand_counts_open_requirements_across_jobs(maker):
    async with maker() as db:
        u = await make_user(db)
        reqs = [{"requirement": "Kafka", "importance": "must", "status": "unknown"},
                {"requirement": "Python", "importance": "must", "status": "met"}]
        await make_match(db, u.id, score=40, confidence=0.5)
        await make_match(db, u.id, score=45, confidence=0.5)
        await db.execute(sa.update(JobMatch).values(requirements=reqs))
        await db.commit()
        assert await interview.demand(db, u.id) == [{"requirement": "Kafka", "jobs": 2, "required_by": 2}]


async def test_answering_saves_facts_drops_questions_and_requeues_audit(maker):
    async with maker() as db:
        u = await make_user(db)
        await interview.run_audit(db, FakeAuditAI(), u.id)
        version = (await db.get(Profile, u.id)).version
        out = await interview.answer(db, u.id, [
            {"question": "What did you own at Acme?", "answer": "The billing service, 2M req/day."},
            {"question": "Notice period?", "skip": True},
            {"question": "Not a pending question", "answer": "ignored"},
        ])
        assert out == {"answered": 1, "skipped": 1}
        facts = (await db.scalars(sa.select(ProfileFact))).all()
        assert [(f.source, f.question) for f in facts] == [("interview", "What did you own at Acme?")]
        p = await db.get(Profile, u.id)
        assert p.version == version + 1 and [q["question"] for q in p.audit["questions"]] == ["Notice period?"]
        assert p.audit["skipped"] == ["Notice period?"]
        assert (await db.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.kind == "audit_profile"))) == 1

        ai = FakeAuditAI()
        await interview.run_audit(db, ai, u.id)
        assert ai.seen["skipped"] == ["Notice period?"] and ai.seen["facts"][0][0] == "What did you own at Acme?"


async def test_audit_keeps_queue_order_and_appends_only_new_questions(maker):
    async with maker() as db:
        u = await make_user(db)
        await interview.run_audit(db, FakeAuditAI(), u.id)
        ai = FakeAuditAI(questions=["Why this next role?", "notice  period?", "What did you own at Acme?"])
        await interview.run_audit(db, ai, u.id)
        qs = [q["question"] for q in (await db.get(Profile, u.id)).audit["questions"]]
        assert qs == ["What did you own at Acme?", "Notice period?", "Why this next role?"]
        assert ai.seen["queued"] == ["What did you own at Acme?", "Notice period?"]


async def test_answer_during_audit_is_not_readded_and_skip_survives(maker):
    async with maker() as db:
        u = await make_user(db)
        await interview.run_audit(db, FakeAuditAI(), u.id)

    async def act():
        async with maker() as other:
            await interview.answer(other, u.id, [
                {"question": "What did you own at Acme?", "answer": "Billing."},
                {"question": "Notice period?", "skip": True},
            ])

    async with maker() as db:
        await interview.run_audit(db, FakeAuditAI(during=act), u.id)
        p = await db.get(Profile, u.id)
        assert [q["question"] for q in p.audit["questions"]] == ["Notice period?"]
        assert p.audit["skipped"] == ["Notice period?"]


async def test_skip_only_moves_question_to_back_without_new_audit(maker):
    async with maker() as db:
        u = await make_user(db)
        await interview.run_audit(db, FakeAuditAI(), u.id)
        await interview.answer(db, u.id, [{"question": "What did you own at Acme?", "skip": True}])
        p = await db.get(Profile, u.id)
        assert [q["question"] for q in p.audit["questions"]] == ["Notice period?", "What did you own at Acme?"]
        assert (await db.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.kind == "audit_profile"))) == 0


async def test_interview_api_view_and_answers(authed):
    await authed.put("/api/profile/background", json={"summary": "SWE"})
    r = await authed.get("/api/profile/interview")
    assert r.status_code == 200 and r.json()["audited"] is False and r.json()["questions"] == []
    assert (await authed.post("/api/profile/interview/refresh")).status_code == 202
    assert (await authed.post("/api/profile/interview/answers", json={"answers": [{"question": "x", "answer": "y"}]})).json()["answered"] == 0


async def test_audit_retires_unreturned_questions_but_keeps_the_head(maker):
    async with maker() as db:
        u = await make_user(db)
        await interview.run_audit(db, FakeAuditAI(questions=["A?", "B?", "C?"]), u.id)
        await interview.run_audit(db, FakeAuditAI(questions=["C?", "D?"]), u.id)
        qs = [q["question"] for q in (await db.get(Profile, u.id)).audit["questions"]]
        assert qs == ["A?", "C?", "D?"]  # A is on screen so it stays; B was retired


async def test_new_questions_come_before_skipped_and_skipped_are_trimmed_first(maker, monkeypatch):
    monkeypatch.setattr(interview, "QUEUE_LIMIT", 4)
    async with maker() as db:
        u = await make_user(db)
        await interview.run_audit(db, FakeAuditAI(questions=["A?", "B?", "C?"]), u.id)
        await interview.answer(db, u.id, [{"question": "B?", "skip": True}])
        await interview.run_audit(db, FakeAuditAI(questions=["A?", "B?", "C?", "D?", "E?"]), u.id)
        qs = [q["question"] for q in (await db.get(Profile, u.id)).audit["questions"]]
        assert qs == ["A?", "C?", "D?", "E?"]  # skipped B is trimmed, not the new questions


async def test_same_question_twice_in_one_request_is_handled_once(maker):
    async with maker() as db:
        u = await make_user(db)
        await interview.run_audit(db, FakeAuditAI(), u.id)
        out = await interview.answer(db, u.id, [
            {"question": "What did you own at Acme?", "answer": "Billing."},
            {"question": "What did you own at Acme?", "skip": True},
        ])
        assert out == {"answered": 1, "skipped": 0}
        p = await db.get(Profile, u.id)
        assert [q["question"] for q in p.audit["questions"]] == ["Notice period?"]
        assert (await db.scalar(sa.select(sa.func.count()).select_from(ProfileFact))) == 1
