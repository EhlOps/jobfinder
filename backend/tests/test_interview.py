import pytest
import sqlalchemy as sa
from factories import make_match, make_user
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.ai.schemas import AuditQuestion, Dossier, ProfileAudit
from jobfinder.models import JobMatch, Profile, ProfileFact, Task
from jobfinder.profile import interview


class AuditAI:
    def __init__(self):
        self.seen = None

    async def audit_profile(self, status, background, facts, demand, skipped):
        self.seen = {"facts": facts, "demand": demand, "skipped": skipped}
        return ProfileAudit(
            dossier=Dossier(headline="Backend engineer", hire_view="I could hire this person for backend roles."),
            readiness=62, dimensions={"skills": 70, "impact": 40},
            questions=[AuditQuestion(question="What did you own at Acme?", why="scope", dimension="scope"),
                       AuditQuestion(question="Notice period?", why="logistics", dimension="logistics")],
        )


@pytest.fixture
def maker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def test_audit_stores_dossier_questions_and_queues_matching(maker):
    async with maker() as db:
        u = await make_user(db)
        out = await interview.run_audit(db, AuditAI(), u.id)
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
        await interview.run_audit(db, AuditAI(), u.id)
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
        assert p.version == version + 1 and p.audit["questions"] == [] and p.audit["skipped"] == ["Notice period?"]
        assert (await db.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.kind == "audit_profile"))) == 1

        ai = AuditAI()
        await interview.run_audit(db, ai, u.id)
        assert ai.seen["skipped"] == ["Notice period?"] and ai.seen["facts"][0][0] == "What did you own at Acme?"


async def test_interview_api_view_and_answers(authed):
    await authed.put("/api/profile/background", json={"summary": "SWE"})
    r = await authed.get("/api/profile/interview")
    assert r.status_code == 200 and r.json()["audited"] is False and r.json()["questions"] == []
    assert (await authed.post("/api/profile/interview/refresh")).status_code == 202
    assert (await authed.post("/api/profile/interview/answers", json={"answers": [{"question": "x", "answer": "y"}]})).json()["answered"] == 0
