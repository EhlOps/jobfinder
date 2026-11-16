"""The retrieval index (#126): index_jobs, index_profile and embed_sweep on the embed queue, the ingest hook
that queues index_jobs, the cron row and `jobfinder ops reindex`, with FakeEmbedder for the model."""
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from jobfinder.core.config import get_settings
from jobfinder.integrations import embed
from jobfinder.models import (
    Job,
    JobEmbedding,
    Profile,
    ProfileEmbedding,
    ProfileFact,
    ProfileVersion,
    Task,
    User,
)
from jobfinder.scheduler import cron
from jobfinder.services.matching import index
from jobfinder.services.matching.embed_text import job_doc, profile_queries
from jobfinder.tasks import embed as embed_tasks
from jobfinder.tasks import io as io_tasks
from jobfinder.tasks import ledger
from tests.cli_runner import invoke
from tests.fakes import FakeEmbedder, run_task

MODEL = "test-model-v1"
T0 = datetime(2026, 11, 1, tzinfo=UTC)
BACKGROUND = {"summary": "Backend engineer building Python services.", "skills": [{"name": "Python"}, {"name": "Postgres"}]}
STATUS = {"target_roles": ["Backend Engineer", "Platform Engineer"]}


@pytest.fixture
def embedder(monkeypatch):
    fake = FakeEmbedder(model=MODEL)
    s = get_settings()
    monkeypatch.setattr(s, "embed_model", MODEL)
    monkeypatch.setattr(s, "embed_batch", 2)
    monkeypatch.setattr(embed, "get_embedder", lambda: fake)
    return fake


_n = 0


async def make_job(db, *, title="Backend Engineer", company="Acme", description="Python and Postgres services.",
                   active=True, seen=0):
    global _n
    _n += 1
    job = Job(source="greenhouse", external_id=f"emb{_n}", company_name=company, title=title, url=f"https://x/{_n}",
              location="Boston, MA", description_text=description, dedupe_hash=f"emb{_n}", is_active=active,
              first_seen_at=T0 + timedelta(minutes=seen))
    db.add(job)
    await db.flush()
    return job


async def make_user(db, *, status=None, background=None, version=1) -> User:
    global _n
    _n += 1
    user = User(email=f"emb{_n}@x.com", password_hash="x")
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, status=STATUS if status is None else status,
                   background=BACKGROUND if background is None else background, version=version))
    await db.flush()
    return user


def source_hash(job: Job) -> str:
    return hashlib.md5(f"{job.title}|{job.company_name}|{job.description_text}".encode()).hexdigest()


async def queued(db, kind=None) -> list[Task]:
    q = sa.select(Task).where(Task.status == "queued").order_by(Task.id)
    return list(await db.scalars(q.where(Task.kind == kind) if kind else q))


async def run_queued(maker, kind) -> list[Task]:
    async with maker() as db:
        ids = [t.id for t in await queued(db, kind)]
    for task_id in ids:
        await run_task(task_id, session_factory=maker)
    async with maker() as db:
        return [await db.get(Task, i) for i in ids]


# ── index_jobs ───────────────────────────────────────────────────────────
async def test_index_jobs_embeds_active_jobs_newest_first_and_chains(maker, embedder, monkeypatch):
    async with maker() as db:
        jobs = [await make_job(db, title=f"Engineer {i}", seen=i) for i in range(5)]
        inactive = await make_job(db, active=False, seen=99)
        task = await ledger.add(db, "index_jobs")
        await db.commit()
    monkeypatch.setattr(get_settings(), "embed_batch", 1)
    monkeypatch.setattr(embed_tasks, "JOB_BATCHES_PER_RUN", 2)  # 2 per run: fewer than the 5 jobs, so it chains
    await run_task(task.id, session_factory=maker)
    async with maker() as db:
        done = await db.get(Task, task.id)
        rows = {r.job_id: r for r in await db.scalars(sa.select(JobEmbedding))}
        [follow_up] = await queued(db, "index_jobs")
    assert done.status == "done" and done.result["embedded"] == 2 and done.result["more"] is True
    assert done.result["next"] == follow_up.id and follow_up.queue == "embed"
    assert set(rows) == {jobs[4].id, jobs[3].id}  # the newest two
    assert embedder.calls == [("documents", 1), ("documents", 1)]  # one call per batch

    [second] = await run_queued(maker, "index_jobs")
    assert (second.result["embedded"], second.result["more"]) == (2, True)
    [last] = await run_queued(maker, "index_jobs")  # the chain ends once nothing is stale
    assert (last.status, last.result["embedded"], last.result["more"]) == ("done", 1, False)
    assert "next" not in last.result
    async with maker() as db:
        rows = {r.job_id: r for r in await db.scalars(sa.select(JobEmbedding))}
        assert await queued(db, "index_jobs") == []
    assert set(rows) == {j.id for j in jobs} and inactive.id not in rows
    for j in jobs:
        assert (rows[j.id].model, rows[j.id].source_hash) == (MODEL, source_hash(j))
        expected = embedder.embed_documents([job_doc({"title": j.title, "company_name": j.company_name,
                                                      "description_text": j.description_text})])[0]
        assert list(rows[j.id].embedding) == pytest.approx(expected, abs=1e-6)


async def test_a_changed_description_is_embedded_again_an_unchanged_one_is_not(db, embedder):
    changed, same = await make_job(db, seen=1), await make_job(db, seen=2)
    out = await index.index_jobs(db, embedder, model=MODEL, limit=10, batch=10)
    assert (out["embedded"], out["more"]) == (2, False)
    changed.description_text = "Rust and Kafka pipelines."
    await db.flush()
    calls = len(embedder.calls)
    out = await index.index_jobs(db, embedder, model=MODEL, limit=10, batch=10)
    assert (out["embedded"], out["more"]) == (1, False) and embedder.calls[calls:] == [("documents", 1)]
    row = await db.get(JobEmbedding, (changed.id, MODEL))
    await db.refresh(row)
    assert row.source_hash == source_hash(changed)
    assert list(row.embedding) == pytest.approx(embedder._vector(job_doc({
        "title": changed.title, "company_name": changed.company_name, "description_text": changed.description_text})),
        abs=1e-6)
    assert (await db.get(JobEmbedding, (same.id, MODEL))).source_hash == source_hash(same)
    assert (await index.index_jobs(db, embedder, model=MODEL, limit=10, batch=10))["embedded"] == 0


async def test_another_models_vectors_dont_count_and_a_mismatched_model_is_refused(db, embedder):
    job = await make_job(db)
    db.add(JobEmbedding(job_id=job.id, model="older-model", source_hash=source_hash(job), embedding=[0.0] * 384))
    await db.flush()
    assert (await index.index_jobs(db, embedder, model=MODEL, limit=10, batch=10))["embedded"] == 1
    with pytest.raises(ValueError, match="loaded model"):
        await index.index_jobs(db, FakeEmbedder(model="other"), model=MODEL, limit=10, batch=10)


# ── index_profile ────────────────────────────────────────────────────────
async def test_index_profile_writes_vectors_and_a_snapshot_and_drops_old_versions(maker, embedder):
    async with maker() as db:
        user = await make_user(db, version=2)
        db.add_all([ProfileFact(user_id=user.id, question="Kafka?", answer="Yes, at Acme"),
                    ProfileFact(user_id=user.id, question="Go?", answer="")])
        db.add(ProfileEmbedding(user_id=user.id, model=MODEL, profile_version=1, ordinal=0, text="old",
                                embedding=[0.0] * 384))
        db.add(ProfileEmbedding(user_id=user.id, model="older-model", profile_version=1, ordinal=0, text="old",
                                embedding=[0.0] * 384))
        task = await ledger.add(db, "index_profile", user_id=user.id)
        await db.commit()
    await run_task(task.id, session_factory=maker)
    queries = profile_queries({"status": STATUS, "background": BACKGROUND})
    async with maker() as db:
        done = await db.get(Task, task.id)
        rows = list(await db.scalars(sa.select(ProfileEmbedding).where(ProfileEmbedding.model == MODEL)
                                     .order_by(ProfileEmbedding.ordinal)))
        snapshot = await db.get(ProfileVersion, (user.id, 2))
        other = await db.scalar(sa.select(sa.func.count()).select_from(ProfileEmbedding)
                                .where(ProfileEmbedding.model == "older-model"))
    assert done.status == "done" and done.result == {"model": MODEL, "version": 2, "queries": 3, "deleted": 1}
    assert len(queries) == 3 and [(r.profile_version, r.ordinal, r.text) for r in rows] == [
        (2, i, q) for i, q in enumerate(queries)]
    assert list(rows[0].embedding) == pytest.approx(embedder._vector(queries[0]), abs=1e-6)
    assert embedder.calls == [("queries", 3)]
    assert (snapshot.status, snapshot.background) == (STATUS, BACKGROUND)
    assert snapshot.facts == [{"question": "Kafka?", "answer": "Yes, at Acme"}]  # answered only
    assert other == 1  # another model's vectors are that model's business

    async with maker() as db:
        task = await ledger.add(db, "index_profile", user_id=user.id)
        await db.commit()
    await run_task(task.id, session_factory=maker)
    async with maker() as db:
        assert (await db.get(Task, task.id)).result["queries"] == 0  # idempotent
        assert await db.scalar(sa.select(sa.func.count()).select_from(ProfileEmbedding)
                               .where(ProfileEmbedding.model == MODEL)) == 3
        assert await db.scalar(sa.select(sa.func.count()).select_from(ProfileVersion)) == 1
    assert embedder.calls == [("queries", 3)]


async def test_an_empty_profile_gets_a_snapshot_but_no_vectors_and_is_not_swept(db, embedder):
    user = await make_user(db, status={}, background={})
    out = await index.index_profile(db, embedder, user.id, model=MODEL)
    assert out["queries"] == 0 and embedder.calls == []
    assert await db.get(ProfileVersion, (user.id, 1)) is not None
    assert (await index.sweep(db, model=MODEL))["index_profile"] == []
    assert await queued(db, "index_profile") == []


# ── embed_sweep and the ingest hook ──────────────────────────────────────
async def test_the_sweep_queues_what_is_due_once(maker, embedder):
    async with maker() as db:
        await make_job(db)
        user = await make_user(db)
        empty = await make_user(db, status={}, background={})
        task = await ledger.add(db, "embed_sweep")
        await db.commit()
    await run_task(task.id, session_factory=maker)
    async with maker() as db:
        done = await db.get(Task, task.id)
        rows = [(t.kind, t.user_id, t.queue) for t in await queued(db)]
    assert done.result == {"index_jobs": True, "index_profile": [user.id], "pruned": 0}
    assert rows == [("index_jobs", None, "embed"), ("index_profile", user.id, "embed")]
    assert empty.id not in done.result["index_profile"]

    async with maker() as db:  # a second sweep before they ran: deduped onto the queued rows
        await index.sweep(db, model=MODEL)
        assert len(await queued(db)) == 2
        await db.rollback()


async def test_the_sweep_queues_a_profile_again_after_an_edit(db, embedder):
    user = await make_user(db)
    await index.index_profile(db, embedder, user.id, model=MODEL)
    assert (await index.sweep(db, model=MODEL))["index_profile"] == []
    (await db.get(Profile, user.id)).version = 2
    await db.flush()
    assert (await index.sweep(db, model=MODEL))["index_profile"] == [user.id]
    out = await index.index_profile(db, embedder, user.id, model=MODEL)
    assert (out["version"], out["queries"], out["deleted"]) == (2, 3, 3)


async def test_the_sweep_drops_vectors_of_postings_gone_inactive(db, embedder):
    live, gone = await make_job(db), await make_job(db)
    await index.index_jobs(db, embedder, model=MODEL, limit=10, batch=10)
    gone.is_active = False
    await db.flush()
    out = await index.sweep(db, model=MODEL)
    assert (out["pruned"], out["index_jobs"]) == (1, False)
    assert [r.job_id for r in await db.scalars(sa.select(JobEmbedding))] == [live.id]


async def test_ingest_queues_a_follow_up_behind_a_running_index_jobs(db):
    running = await ledger.claim(db, (await ledger.add(db, "index_jobs")).id)
    await io_tasks._changed(db, SimpleNamespace(new=1, updated=0, deactivated=0))
    [follow_up] = await queued(db, "index_jobs")  # the running one may have checked before these postings
    assert follow_up.id != running.id


@pytest.mark.parametrize("new, queues", [(3, True), (0, False)])
async def test_ingest_queues_index_jobs_when_postings_are_new(db, monkeypatch, new, queues):
    monkeypatch.setattr(get_settings(), "adaptive_schedule_enabled", True)
    stats = SimpleNamespace(new=new, updated=5, deactivated=0)
    await io_tasks._changed(db, stats)
    await io_tasks._changed(db, stats)  # a second board finishing: deduped
    assert [t.queue for t in await queued(db, "index_jobs")] == (["embed"] if queues else [])


def test_the_sweep_is_on_the_cron_table():
    [row] = [c for c in cron.TABLE if c.kind == "embed_sweep"]
    jobs = {j.id: j for j in cron.make_scheduler().get_jobs()}
    assert row.trigger == "interval" and "interval[0:05:00]" in str(jobs["embed_sweep"].trigger)


# ── ops reindex ──────────────────────────────────────────────────────────
@pytest.mark.real_commit
async def test_ops_reindex_drops_the_models_vectors_and_queues_a_sweep(maker, embedder):
    async with maker() as db:
        job = await make_job(db)
        user = await make_user(db)
        await index.index_jobs(db, embedder, model=MODEL, limit=10, batch=10)
        await index.index_profile(db, embedder, user.id, model=MODEL)
        db.add(JobEmbedding(job_id=job.id, model="other", source_hash="x" * 32, embedding=[0.0] * 384))
        await db.commit()
    r = await invoke("ops", "reindex")
    assert r.exit_code == 0, r.output
    assert f"Deleted 1 posting and 3 profile vectors of {MODEL}" in r.stdout
    async with maker() as db:
        assert [r.model for r in await db.scalars(sa.select(JobEmbedding))] == ["other"]
        assert await db.scalar(sa.select(sa.func.count()).select_from(ProfileEmbedding)) == 0
        assert await db.get(ProfileVersion, (user.id, 1)) is not None  # not derived: kept
        [sweep] = await queued(db, "embed_sweep")
    assert sweep.queue == "embed"
    r = await invoke("ops", "reindex", "--model", "other")
    assert r.exit_code == 0 and "Deleted 1 posting and 0 profile vectors of other" in r.stdout
