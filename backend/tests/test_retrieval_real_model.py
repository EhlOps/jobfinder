"""The whole retrieval path with the real model (#126): embed_sweep queues the indexing tasks, worker-embed's
handlers embed the postings and the profile with bge-small-jobfinder-v2, then match_user retrieves by vector and
Claude (faked) scores the nearest ones (or, with `hybrid`, the prefilter's best by the blend). Same postings as test_retrieval_e2e, but no hand-made vectors: this is
what proves the real model ranks a backend engineer's postings first and the hard filters still hold.

Needs the `embed` extra and EMBED_MODEL_DIR pointing at the model directory; skips otherwise. Run it with
`pytest -m embed_model`.
"""
from pathlib import Path

import pytest

pytest.importorskip("fastembed")

import sqlalchemy as sa

from jobfinder.core.config import get_settings, settings
from jobfinder.models import Job, Profile, Task, User
from jobfinder.models.embedding import JobEmbedding, MatchReview, ProfileEmbedding
from jobfinder.tasks import ledger
from tests.fakes import FakeScoringAI, run_task
from tests.test_retrieval_e2e import BACKEND_TITLES, BACKGROUND, EXCLUDED, FIELDS, STATUS

pytestmark = pytest.mark.embed_model


@pytest.fixture(autouse=True)
def real_model(monkeypatch):
    if not (Path(settings.embed_model_dir) / "model.onnx").is_file():
        pytest.skip(f"no model.onnx in EMBED_MODEL_DIR={settings.embed_model_dir}")
    s = get_settings()
    for name, value in {"match_retrieval": "vector", "match_retrieval_k": 200, "match_slice_jobs": 25,
                        "ai_max_concurrency": 2}.items():
        monkeypatch.setattr(s, name, value)


async def _drain(maker, ai=None):
    """Run every queued task in id order, including the ones they queue, the way the workers would."""
    while True:
        async with maker() as db:
            tid = await db.scalar(sa.select(Task.id).where(Task.status == "queued").order_by(Task.id))
        if tid is None:
            return
        await run_task(tid, ai, maker)


@pytest.mark.parametrize("engine", ["vector", "hybrid"])
async def test_real_model_indexes_then_retrieves_the_backend_postings(maker, monkeypatch, engine):
    monkeypatch.setattr(get_settings(), "match_retrieval", engine)
    async with maker() as db:
        jobs = []
        for field, postings in FIELDS.items():
            for title, desc in postings:
                jobs.append(Job(source="greenhouse", external_id=f"{field}-{len(jobs)}", company_name="Acme",
                                title=title, url=f"https://jobs.example/{len(jobs)}", location="Boston, MA",
                                workplace_type="onsite", description_text=desc, dedupe_hash=f"rm{len(jobs)}"))
        for title, kw in EXCLUDED.items():
            jobs.append(Job(source="greenhouse", external_id=f"x-{len(jobs)}", company_name="Acme", title=title,
                            url=f"https://jobs.example/{len(jobs)}", dedupe_hash=f"rm{len(jobs)}",
                            **{"location": "Boston, MA", "workplace_type": "onsite",
                               "description_text": "Build Python REST APIs on Postgres and Kafka.", **kw}))
        db.add_all(jobs)
        user = User(email="backend@x.com", password_hash="x", match_budget=5)
        db.add(user)
        await db.flush()
        db.add(Profile(user_id=user.id, status=STATUS, background=BACKGROUND, version=1))
        await ledger.add(db, "embed_sweep")
        await db.commit()

    await _drain(maker)  # embed_sweep -> index_jobs + index_profile, on the real model
    async with maker() as db:
        failed = list(await db.scalars(sa.select(Task).where(Task.status != "done")))
        assert not failed, [(t.kind, t.error) for t in failed]
        models = set(await db.scalars(sa.select(JobEmbedding.model))) | set(
            await db.scalars(sa.select(ProfileEmbedding.model)))
        assert models == {settings.embed_model}
        n_active = sum(1 for j in jobs if j.is_active is not False)
        assert await db.scalar(sa.select(sa.func.count()).select_from(JobEmbedding)) == n_active

        task = await ledger.add(db, "match_user", user_id=user.id)
        await db.commit()
    ai = FakeScoringAI()
    await run_task(task.id, ai, maker)
    await _drain(maker, ai)

    assert len(ai.calls) == 5
    assert set(ai.calls) <= BACKEND_TITLES, ai.calls  # the model (and the prefilter) put backend postings first
    assert not set(ai.calls) & set(EXCLUDED)  # remote, no-visa and closed never reach Claude
    async with maker() as db:
        rows = list(await db.scalars(sa.select(MatchReview).order_by(MatchReview.id)))
    assert [(r.engine, r.model, r.retrieval_rank) for r in rows] == [
        (engine, settings.embed_model, i) for i in range(5)]
    scores = [r.retrieval_score for r in rows]
    assert min(scores) > 0.3
    if engine == "vector":
        assert scores == sorted(scores, reverse=True)
