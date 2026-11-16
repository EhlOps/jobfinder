"""Vector retrieval end to end (#126): ~30 postings across five fields, a backend engineer's profile, FakeEmbedder
vectors, and match_user through the real task path with a daily budget of 5."""
import pytest
import sqlalchemy as sa

from jobfinder.core.config import get_settings
from jobfinder.models import Job, Profile, Task, User
from jobfinder.models.embedding import JobEmbedding, MatchReview, ProfileEmbedding
from jobfinder.services.matching.embed_text import job_doc, profile_queries
from jobfinder.tasks import ledger
from tests.fakes import FakeEmbedder, FakeScoringAI, run_task

MODEL = "test-embedder"
STATUS = {"target_roles": ["Backend Engineer"], "target_locations": ["Boston"], "remote_preference": "onsite",
          "is_new_grad": True, "needs_visa_sponsorship": True}
BACKGROUND = {
    "summary": "Backend engineer building Python APIs and Postgres data services.",
    "skills": [{"name": s} for s in ("Python", "Postgres", "Kafka", "Docker", "REST APIs")],
}
FIELDS = {
    "backend": [
        ("Backend Engineer", "Build Python REST APIs on Postgres and Kafka."),
        ("Backend Software Engineer", "Python services, Postgres, Docker and Kafka pipelines."),
        ("Python Backend Developer", "REST APIs in Python with Postgres."),
        ("Backend Engineer, Payments", "Payments APIs in Python on Postgres and Kafka."),
        ("Software Engineer, Backend", "Python backend services, Docker, Postgres."),
        ("Backend Engineer, Platform", "Platform services in Python, Kafka and Postgres."),
        ("API Engineer", "REST APIs in Python, Postgres and Docker."),
        ("Backend Developer", "Python Kafka consumers and Postgres schemas."),
        ("Data Platform Backend Engineer", "Python services feeding Postgres and Kafka."),
        ("Junior Backend Engineer", "Python APIs and Postgres queries, Docker deploys."),
    ],
    "frontend": [
        ("Frontend Engineer", "React and TypeScript user interfaces, CSS."),
        ("UI Developer", "Design systems in React, accessible CSS components."),
        ("Frontend Developer", "TypeScript, React hooks and browser performance."),
        ("Web Designer", "Figma mockups, CSS layouts and brand visuals."),
    ],
    "data": [
        ("Data Analyst", "Dashboards in Tableau, Excel models, stakeholder reports."),
        ("Business Intelligence Analyst", "Tableau dashboards and KPI reporting."),
        ("Data Scientist", "Statistical models, experiments and notebooks in R."),
        ("Analytics Associate", "Excel reporting and quarterly business reviews."),
    ],
    "nursing": [
        ("Registered Nurse", "Patient care on a busy hospital ward."),
        ("ICU Nurse", "Critical care nursing, patient monitoring, night shifts."),
        ("Nurse Practitioner", "Primary care clinic, patient assessments, prescriptions."),
        ("Pediatric Nurse", "Caring for children and families in the hospital."),
    ],
    "sales": [
        ("Account Executive", "Closing deals, quota attainment, CRM pipeline."),
        ("Sales Development Representative", "Cold calls, outbound prospecting, quotas."),
        ("Account Manager", "Client relationships, renewals and upsells."),
    ],
}
BACKEND_TITLES = {t for t, _ in FIELDS["backend"]}
EXCLUDED = {  # as close to the profile as any backend posting, but never to be scored
    "Remote Backend Engineer": {"location": "Remote", "workplace_type": "remote"},  # the user wants on-site
    "Backend Engineer, No Visa": {"description_text": "Build Python REST APIs on Postgres and Kafka. "
                                                      "We are unable to sponsor visas for this role."},
    "Backend Engineer, Closed": {"is_active": False},
}


async def seed(db, emb: FakeEmbedder, model: str = MODEL) -> User:
    """The postings, the user and both sides' vectors (what the indexing tasks would write)."""
    jobs = []
    for field, postings in FIELDS.items():
        for title, desc in postings:
            jobs.append(Job(source="greenhouse", external_id=f"{field}-{len(jobs)}", company_name="Acme", title=title,
                            url=f"https://jobs.example/{len(jobs)}", location="Boston, MA", workplace_type="onsite",
                            description_text=desc, dedupe_hash=f"e2e{len(jobs)}"))
    for title, kw in EXCLUDED.items():
        jobs.append(Job(source="greenhouse", external_id=f"x-{len(jobs)}", company_name="Acme", title=title,
                        url=f"https://jobs.example/{len(jobs)}", dedupe_hash=f"e2e{len(jobs)}",
                        **{"location": "Boston, MA", "workplace_type": "onsite",
                           "description_text": "Build Python REST APIs on Postgres and Kafka.", **kw}))
    db.add_all(jobs)
    user = User(email="backend@x.com", password_hash="x")
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, status=STATUS, background=BACKGROUND, version=1))
    docs = [job_doc({"title": j.title, "company_name": j.company_name, "description_text": j.description_text})
            for j in jobs]
    for j, v in zip(jobs, emb.embed_documents(docs), strict=True):
        db.add(JobEmbedding(job_id=j.id, model=model, source_hash="x", embedding=v))
    queries = profile_queries({"status": STATUS, "background": BACKGROUND})
    for i, (text, v) in enumerate(zip(queries, emb.embed_queries(queries), strict=True)):
        db.add(ProfileEmbedding(user_id=user.id, model=model, profile_version=1, ordinal=i, text=text, embedding=v))
    user.match_budget = 5
    await db.commit()
    return user


async def _run_chain(maker, ai, user_id):
    async with maker() as db:
        task = await ledger.add(db, "match_user", user_id=user_id)
        await db.commit()
    tid = task.id
    while tid is not None:
        await run_task(tid, ai, maker)
        async with maker() as db:
            tid = await db.scalar(sa.select(Task.id).where(Task.kind == "match_user", Task.status == "queued"))


@pytest.fixture(autouse=True)
def vector(monkeypatch):
    s = get_settings()
    for name, value in {"match_retrieval": "vector", "embed_model": MODEL, "match_retrieval_k": 200,
                        "match_slice_jobs": 25, "ai_max_concurrency": 2}.items():
        monkeypatch.setattr(s, name, value)


async def test_vector_retrieval_end_to_end(maker):
    async with maker() as db:
        user = await seed(db, FakeEmbedder())
        nearest = [
            r.title for r in (await db.execute(sa.text(  # the profile's nearest active postings, straight from SQL
                "select j.title, max(1 - (je.embedding <=> pe.embedding)) as s from job_embeddings je "
                "join jobs j on j.id = je.job_id join profile_embeddings pe on pe.user_id = :u "
                "where j.is_active group by j.title order by s desc, min(j.id)"), {"u": user.id})).all()
            if r.title in BACKEND_TITLES
        ]

    ai = FakeScoringAI()
    await _run_chain(maker, ai, user.id)
    assert len(ai.calls) == 5 and set(ai.calls) <= BACKEND_TITLES
    assert ai.calls == nearest[:5]  # closest first
    async with maker() as db:
        rows = list(await db.scalars(sa.select(MatchReview).order_by(MatchReview.id)))
        assert [(r.engine, r.model, r.retrieval_rank) for r in rows] == [("vector", MODEL, i) for i in range(5)]
        assert all(r.retrieval_score is not None for r in rows)
        (await db.get(User, user.id)).match_budget = 10  # a new day's budget
        await db.commit()

    # the next run picks the next nearest unmatched postings, ranked from 0 again
    await _run_chain(maker, ai, user.id)
    assert len(ai.calls) == 10 and set(ai.calls[5:]) <= BACKEND_TITLES and len(set(ai.calls)) == 10
    assert ai.calls[5:] == nearest[5:10]
    assert not set(ai.calls) & set(EXCLUDED)
    async with maker() as db:
        ranks = list(await db.scalars(sa.select(MatchReview.retrieval_rank).order_by(MatchReview.id)))
        assert ranks == [0, 1, 2, 3, 4] * 2
