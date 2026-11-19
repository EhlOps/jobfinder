"""Vector retrieval (#126): the profile's nearest postings by embedding, the hard filters on top, the fallback
to the prefilter, the match_reviews labels, and `jobfinder ops eval-retrieval`.

Vectors come from FakeEmbedder (bag of words) and are inserted directly; the indexing tasks aren't involved."""
import json
import logging

import pytest
import sqlalchemy as sa

from jobfinder.core.config import get_settings
from jobfinder.integrations.claude.schemas import MatchScore, RequirementCheck
from jobfinder.models import Job, JobMatch, Profile
from jobfinder.models.embedding import JobEmbedding, MatchReview, ProfileEmbedding
from jobfinder.models.posting import dedupe_hash
from jobfinder.repositories import retrieval as retrieval_repo
from jobfinder.services.matching import service
from jobfinder.services.matching.embed_text import job_doc, profile_queries
from tests.cli_runner import invoke
from tests.factories import make_match
from tests.fakes import FakeEmbedder, FakeScoringAI
from tests.test_matching_service import (  # noqa: F401 (budget: autouse)
    STATUS,
    budget,
    make_job,
    make_user,
    matches,
)

MODEL = "test-embedder"
EMB = FakeEmbedder()
BACKEND = {"skills": [{"name": s} for s in ("Python", "Postgres", "Kafka")]}


@pytest.fixture(autouse=True)
def vector(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "match_retrieval", "vector")
    monkeypatch.setattr(s, "embed_model", MODEL)
    monkeypatch.setattr(s, "match_retrieval_k", 50)


async def index_jobs(db, *jobs, model=MODEL):
    docs = [job_doc({"title": j.title, "company_name": j.company_name, "description_text": j.description_text})
            for j in jobs]
    for j, v in zip(jobs, EMB.embed_documents(docs), strict=True):
        db.add(JobEmbedding(job_id=j.id, model=model, source_hash="x", embedding=v))
    await db.commit()


async def index_profile(db, user_id, *, model=MODEL, version=None, queries=None):
    p = await db.get(Profile, user_id)
    queries = queries or profile_queries({"status": p.status, "background": p.background})
    for i, (text, v) in enumerate(zip(queries, EMB.embed_queries(queries), strict=True)):
        db.add(ProfileEmbedding(user_id=user_id, model=model, profile_version=p.version if version is None else version,
                                ordinal=i, text=text, embedding=v))
    await db.commit()


async def backend_user(db, status=None, email="u@x.com"):
    u = await make_user(db, status or STATUS, BACKEND, email=email)
    await index_profile(db, u.id)
    return u


async def topical_jobs(db):
    """Two backend postings (one closer than the other) and three off-topic ones, all in Boston and fine to score."""
    jobs = [
        await make_job(db, "Backend Engineer", description_text="Python Postgres Kafka services."),
        await make_job(db, "Platform Engineer", description_text="Python services on Postgres."),
        await make_job(db, "Registered Nurse", description_text="Patient care in a busy hospital ward."),
        await make_job(db, "Sales Representative", description_text="Cold calls, quotas and a CRM pipeline."),
        await make_job(db, "Pastry Chef", description_text="Croissants, laminated dough and early mornings."),
    ]
    await index_jobs(db, *jobs)
    return jobs


async def reviews(db):
    return list(await db.scalars(sa.select(MatchReview).order_by(MatchReview.retrieval_rank, MatchReview.id)))


async def test_vector_retrieval_ranks_topical_jobs_first_and_keeps_off_topic_ones(maker):
    async with maker() as db:
        u = await backend_user(db)
        await topical_jobs(db)
        r = await service.retrieve(db, u.id, await db.get(Profile, u.id))
        titles = [(await db.get(Job, c.job_id)).title for c in r.candidates]
        assert r.engine == "vector" and r.model == MODEL
        assert titles[:2] == ["Backend Engineer", "Platform Engineer"]
        # no MATCH_MIN_PREFILTER cutoff: the off-topic jobs are still there, just ranked below
        assert set(titles[2:]) == {"Registered Nurse", "Sales Representative", "Pastry Chef"}
        scores = [c.retrieval_score for c in r.candidates]
        assert scores == sorted(scores, reverse=True) and scores[0] > 0.5 > scores[-1]
        ai = FakeScoringAI()
        await service.match_user(db, ai, u.id, budget=2)
        assert ai.calls == ["Backend Engineer", "Platform Engineer"]


async def test_hard_filters_still_exclude(maker):
    async with maker() as db:
        u = await backend_user(db, {**STATUS, "needs_visa_sponsorship": True})
        desc = "Python Postgres Kafka services."
        jobs = [
            await make_job(db, "Backend Engineer", description_text=desc),
            await make_job(db, "Backend Engineer Dublin", location="Dublin, Ireland", description_text=desc),
            await make_job(db, "Backend Engineer Cheap", salary_max=80000, salary_currency="USD", description_text=desc),
            await make_job(db, "Backend Engineer Visa", description_text=desc + " No visa sponsorship available."),
            await make_job(db, "Backend Engineer Senior", seniority="senior", description_text=desc),
            await make_job(db, "Backend Engineer Years", description_text=desc + " Requires 6+ years of experience."),
        ]
        await index_jobs(db, *jobs)
        ai = FakeScoringAI()
        out = await service.match_user(db, ai, u.id)
        assert ai.calls == ["Backend Engineer"] and out["pending"] == 0


async def test_matched_jobs_and_their_postings_are_excluded(maker):
    async with maker() as db:
        u = await backend_user(db)
        h = dedupe_hash("Acme", "Backend Engineer", "Boston, MA")
        first = await make_job(db, "Backend Engineer", dedupe_hash=h, description_text="Python Postgres Kafka services.")
        platform = await make_job(db, "Platform Engineer", description_text="Python services on Postgres.")
        await index_jobs(db, first, platform)
        ai = FakeScoringAI()
        await service.match_user(db, ai, u.id, budget=1)
        assert ai.calls == ["Backend Engineer"]
        copy = await make_job(db, "Backend Engineer", dedupe_hash=h, description_text="Python Postgres Kafka services.")
        await index_jobs(db, copy)  # the same posting on another requisition: closest, but already matched
        rows = await retrieval_repo.nearest_unmatched_jobs(db, u.id, 3, MODEL, 50, None)
        assert [r.id for r in rows] == [platform.id]
        await service.match_user(db, ai, u.id, budget=1)
        assert ai.calls == ["Backend Engineer", "Platform Engineer"]
        assert set(await matches(db)) == {"Backend Engineer", "Platform Engineer"}


async def test_inactive_jobs_and_other_models_vectors_are_ignored(maker):
    async with maker() as db:
        u = await backend_user(db)
        await index_profile(db, u.id, model="other-model", queries=["Registered Nurse. Patient care hospital ward"])
        closed = await make_job(db, "Backend Engineer Closed", is_active=False,
                                description_text="Python Postgres Kafka services.")
        other = await make_job(db, "Backend Engineer Other", description_text="Python Postgres Kafka services.")
        await index_jobs(db, closed)
        await index_jobs(db, other, model="other-model")
        nurse = await make_job(db, "Registered Nurse", description_text="Patient care in a busy hospital ward.")
        await index_jobs(db, nurse)
        await index_jobs(db, nurse, model="other-model")
        rows = await retrieval_repo.nearest_unmatched_jobs(db, u.id, 3, MODEL, 50, None)
        # only the nurse has a test-embedder vector and is active; the other model's nurse query didn't lift it
        assert [r.id for r in rows] == [nurse.id] and rows[0].retrieval_score < 0.5
        assert rows[0]._fields[:9] == ("id", "title", "location", "workplace_type", "salary_max", "salary_currency",
                                       "posted_at", "dedupe_hash", "seniority")


async def test_k_nearest_per_query_vector_unioned_with_the_best_score(maker, monkeypatch):
    monkeypatch.setattr(get_settings(), "match_retrieval_k", 1)
    async with maker() as db:
        u = await make_user(db, {**STATUS, "target_roles": ["Backend Engineer", "Registered Nurse"]})
        await index_profile(db, u.id)
        await topical_jobs(db)
        rows = await retrieval_repo.nearest_unmatched_jobs(db, u.id, 3, MODEL, 1, None)
        assert {r.title for r in rows} == {"Backend Engineer", "Registered Nurse"}  # one per query vector
        settings_now = (await db.execute(sa.text(
            "select current_setting('hnsw.iterative_scan'), current_setting('hnsw.ef_search')"))).one()
        assert tuple(settings_now) == ("relaxed_order", "100")


async def test_falls_back_to_the_prefilter_without_query_vectors(maker, caplog):
    async with maker() as db:
        u = await make_user(db, STATUS, BACKEND)
        await index_profile(db, u.id, version=2)  # an older version's vectors don't count
        await topical_jobs(db)
        caplog.set_level(logging.INFO, "jobfinder.services.matching")
        ai = FakeScoringAI()
        await service.match_user(db, ai, u.id)
        # the prefilter's cutoff drops the off-topic jobs
        assert set(ai.calls) == {"Backend Engineer", "Platform Engineer"}
        assert "no test-embedder query vectors yet, retrieving by prefilter" in caplog.text
        rows = await reviews(db)
        assert [(r.engine, r.model, r.retrieval_rank, r.retrieval_score) for r in rows] == [
            ("prefilter", None, 0, None), ("prefilter", None, 1, None)]


async def test_each_claude_score_leaves_a_review_with_engine_and_rank(maker):
    async with maker() as db:
        u = await backend_user(db)
        await topical_jobs(db)
        req = RequirementCheck(requirement="Python", status="met", importance="must", evidence="5 years")

        class ReqAI(FakeScoringAI):
            async def score_match(self, *a, **kw):
                out = await super().score_match(*a, **kw)
                return MatchScore(**{**out.model_dump(), "requirements": [req]})

        await service.match_user(db, ReqAI({"Backend Engineer": 90, "Platform Engineer": 70}), u.id, budget=3)
        rows = await reviews(db)
        m = {r.job_id: r for r in await db.scalars(sa.select(JobMatch))}
        assert [(r.engine, r.model, r.retrieval_rank) for r in rows] == [("vector", MODEL, i) for i in range(3)]
        assert [r.retrieval_score for r in rows] == sorted((r.retrieval_score for r in rows), reverse=True)
        for r in rows:
            assert r.user_id == u.id and r.profile_version == 3 and r.prefilter_score == m[r.job_id].prefilter_score
            assert (r.llm_score, r.verdict, r.confidence) == (m[r.job_id].llm_score, m[r.job_id].verdict, m[r.job_id].confidence)
            assert r.requirements == m[r.job_id].requirements and r.requirements[0]["requirement"] == "Python"


async def test_stale_rescores_share_the_budget_and_are_labelled_without_a_retrieval_score(maker):
    async with maker() as db:
        u = await backend_user(db)
        jobs = await topical_jobs(db)
        await service.match_user(db, FakeScoringAI(), u.id, budget=1)  # Backend Engineer, at version 3
        p = await db.get(Profile, u.id)
        p.version = 4
        await db.commit()
        await index_profile(db, u.id)
        ai = FakeScoringAI()
        out = await service.match_user(db, ai, u.id, budget=2)
        # interleaved by position: the nearest new job, then the stale re-score
        assert ai.calls == ["Platform Engineer", "Backend Engineer"] and out["scored"] == 2
        assert out["budget_left"] == 0 and out["pending"] == 3
        new = [r for r in await reviews(db) if r.profile_version == 4]
        assert [(r.job_id, r.retrieval_rank, r.retrieval_score is None) for r in new] == [
            (jobs[1].id, 0, False), (jobs[0].id, 1, True)]


async def test_budget_is_respected(maker):
    async with maker() as db:
        u = await backend_user(db)
        await topical_jobs(db)
        get_settings().match_daily_llm_budget = 3
        ai = FakeScoringAI()
        out = await service.match_user(db, ai, u.id)
        assert len(ai.calls) == 3 and (out["scored"], out["pending"], out["budget_left"]) == (3, 2, 0)
        out = await service.match_user(db, ai, u.id)
        assert len(ai.calls) == 3 and out["scored"] == 0


def _c(job_id, score, posted, match_id=None, saved=False, retrieval=None):
    return service.Candidate(job_id, score, posted, f"h{job_id}", match_id=match_id, saved=saved,
                             retrieval_score=retrieval)


def test_rank_work_with_the_vector_engine_interleaves_new_and_stale_by_position():
    stale = [_c(1, 10, 5, match_id=101), _c(2, 90, 5, match_id=102), _c(3, 5, 1, match_id=103, saved=True),
             _c(4, 50, 9, match_id=104), _c(5, 1, 1, match_id=105, saved=True)]
    # retrieve's order (closest first); their prefilter scores don't matter here
    new = [_c(6, 1, 1, retrieval=0.9), _c(7, 99, 10, retrieval=0.8), _c(8, 50, 0, retrieval=0.7)]
    order = [w.job_id for w in service.rank_work(stale, new, answered={105, 101}, engine="vector")]
    # answered, saved (as with the prefilter), then tier 2: new[0], stale[0] (by stored score: 2), new[1], 4, new[2]
    assert order == [1, 5, 3, 6, 2, 7, 4, 8]
    assert [w.job_id for w in service.rank_work([], new, set(), engine="vector")] == [6, 7, 8]
    # the prefilter engine is unchanged: by score, then newest
    assert [w.job_id for w in service.rank_work([], new, set())] == [7, 8, 6]


@pytest.mark.real_commit
async def test_eval_retrieval_compares_the_engines(maker):
    async with maker() as db:
        u = await backend_user(db)
        await topical_jobs(db)
        await service.match_user(db, FakeScoringAI({"Backend Engineer": 90, "Platform Engineer": 70}), u.id, budget=2)
        await db.commit()
        other = await make_user(db, email="no-vectors@x.com")
        await make_match(db, other.id)  # scored, but its profile has no query vectors: skipped
        await db.commit()
    r = await invoke("ops", "eval-retrieval", "--top", "3")
    assert r.exit_code == 0, r.output
    lines = r.stdout.splitlines()
    assert lines[1].split()[:4] == ["user", "ver", "labels", "engine"]
    assert "1 users compared" in r.stdout and f"no query vectors for its current version): {other.id}" in r.stdout
    r = await invoke("ops", "eval-retrieval", "--json", "--top", "2")
    report = json.loads(r.stdout)
    (row,) = report["users"]
    assert report["model"] == MODEL and row["user_id"] == u.id and row["labelled"] == 2
    # matched jobs count as labelled: every engine's top 2 are the two scored backend jobs
    for engine in ("prefilter", "vector"):
        assert (row[engine]["scored"], row[engine]["mean_llm"], row[engine]["good_share"]) == (2, 80.0, 1.0)
        assert row[engine]["best_ranks"] == [1, 2] and row[engine]["overlap"] == 2
    # the hybrid engine ranks the prefilter's three jobs in its own order: Platform Engineer drops to third
    assert row["hybrid"]["best_ranks"] == [1, 3] and row["hybrid"]["overlap"] == 1
    # the prefilter's cutoff drops some off-topic jobs; the vector engine keeps all five, closest first; the
    # hybrid engine reorders the prefilter's
    assert row["vector"]["candidates"] == 5 > row["prefilter"]["candidates"] == row["hybrid"]["candidates"]
    assert row["labels_version"] == row["profile_version"] == 3
    async with maker() as db:
        (await db.get(Profile, u.id)).version = 4  # edited since: no labels at the new version
        await db.commit()
        await index_profile(db, u.id)
    report = json.loads((await invoke("ops", "eval-retrieval", "--json")).stdout)
    assert report["users"][0]["labelled"] == 0
    r = await invoke("ops", "eval-retrieval", "--json", "--labels-version", "3")
    (row,) = json.loads(r.stdout)["users"]
    assert (row["profile_version"], row["labels_version"], row["labelled"]) == (4, 3, 2)
    assert row["hybrid"]["good_share"] == 1.0


# ── hybrid: the prefilter's pool, ordered by the blend ───────────────────────
@pytest.fixture
def hybrid(monkeypatch):
    monkeypatch.setattr(get_settings(), "match_retrieval", "hybrid")
    monkeypatch.setattr(get_settings(), "match_hybrid_weight", 0.5)


async def title_vs_content_jobs(db):
    """The prefilter likes the first (its title is the target role) and the model the second (its text is the
    profile's skills); the third is middling on both."""
    jobs = [
        await make_job(db, "Backend Engineer", description_text="Croissants, laminated dough and early mornings."),
        await make_job(db, "Software Developer", description_text="Python Postgres Kafka. Python Postgres Kafka."),
        await make_job(db, "Backend Developer", description_text="Python services."),
    ]
    await index_jobs(db, *jobs)
    return jobs


def _z(xs):
    m = sum(xs) / len(xs)
    sd = (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5 or 1.0
    return [(x - m) / sd for x in xs]


async def test_pool_cosines_is_the_exact_best_cosine_over_the_query_vectors(maker):
    async with maker() as db:
        u = await make_user(db, {**STATUS, "target_roles": ["Backend Engineer", "Registered Nurse"]}, BACKEND)
        await index_profile(db, u.id)
        backend, _platform, nurse, sales, _chef = await topical_jobs(db)
        unindexed = await make_job(db, "Backend Engineer Unindexed")
        await index_jobs(db, sales, model="other-model")
        got = await retrieval_repo.pool_cosines(db, u.id, 3, MODEL, [backend.id, nurse.id, sales.id, unindexed.id])
        assert set(got) == {backend.id, nurse.id, sales.id}  # no vector, no entry; one per job across models
        near = {r.id: r.retrieval_score for r in await retrieval_repo.nearest_unmatched_jobs(db, u.id, 3, MODEL, 50, None)}
        for job_id, cos in got.items():
            assert cos == pytest.approx(near[job_id])  # the same max cosine the HNSW path reports
        assert await retrieval_repo.pool_cosines(db, u.id, 3, MODEL, []) == {}
        assert await retrieval_repo.pool_cosines(db, u.id, 2, MODEL, [backend.id]) == {}  # another version


def test_blend_z_scores_both_and_a_job_without_a_vector_counts_as_average():
    cands = [_c(1, 80, 0, retrieval=0.2), _c(2, 40, 0, retrieval=0.8), _c(3, 60, 0, retrieval=0.5), _c(4, 60, 0)]
    b = service.Blend.fit(cands, 0.5)
    zp, zc = _z([80, 40, 60, 60]), _z([0.2, 0.8, 0.5])
    expected = [0.5 * zc[0] + 0.5 * zp[0], 0.5 * zc[1] + 0.5 * zp[1], 0.5 * zc[2] + 0.5 * zp[2], 0.5 * zp[3]]
    assert [b(c) for c in cands] == pytest.approx(expected)
    assert max(cands, key=service.Blend.fit(cands, 0)).job_id == 1  # weight 0: the prefilter's order
    assert max(cands, key=service.Blend.fit(cands, 1)).job_id == 2  # weight 1: the cosine's
    assert service.Blend.fit(cands, 7).weight == 1.0  # clamped
    same = [_c(1, 50, 0, retrieval=0.5), _c(2, 50, 0, retrieval=0.5)]
    assert [service.Blend.fit(same, 0.5)(c) for c in same] == [0, 0]  # sd 0: no division by zero


async def test_hybrid_keeps_the_prefilter_pool_and_orders_it_by_the_blend(maker, hybrid):
    async with maker() as db:
        u = await backend_user(db)
        jobs = await title_vs_content_jobs(db)
        await topical_jobs(db)
        p = await db.get(Profile, u.id)
        pool = await service.prefilter_candidates(db, u.id, p)
        r = await service.retrieve(db, u.id, p)
        assert r.engine == "hybrid" and r.model == MODEL
        assert {c.job_id for c in r.candidates} == {c.job_id for c in pool}  # same jobs, same cutoff
        zp, zc = _z([c.score for c in r.candidates]), _z([c.retrieval_score for c in r.candidates])
        blends = [0.5 * a + 0.5 * b for a, b in zip(zc, zp, strict=True)]
        assert [c.blend for c in r.candidates] == pytest.approx(blends)
        assert blends == sorted(blends, reverse=True)
        order = [c.job_id for c in r.candidates]
        assert order != [c.job_id for c in pool]  # the blend really reorders
        assert order.index(jobs[1].id) < order.index(jobs[0].id)  # skills-heavy text beats a bare title match
        ai = FakeScoringAI()
        await service.match_user(db, ai, u.id, budget=2)
        rows = await reviews(db)
        assert [(r.engine, r.model, r.retrieval_rank) for r in rows] == [("hybrid", MODEL, 0), ("hybrid", MODEL, 1)]
        assert [r.job_id for r in rows] == order[:2] and all(r.retrieval_score is not None for r in rows)


async def test_hybrid_without_a_background_or_vectors_uses_the_prefilter(maker, hybrid, caplog):
    caplog.set_level(logging.INFO, "jobfinder.services.matching")
    async with maker() as db:
        titles_only = await make_user(db, STATUS, {}, email="titles@x.com")
        await index_profile(db, titles_only.id)
        unindexed = await make_user(db, STATUS, BACKEND, email="new@x.com")
        await topical_jobs(db)
        for u in (titles_only, unindexed):
            r = await service.retrieve(db, u.id, await db.get(Profile, u.id))
            assert r.engine == "prefilter" and r.model is None and all(c.blend is None for c in r.candidates)
        assert f"user {titles_only.id}: no background skills or summary, retrieving by prefilter" in caplog.text
        assert f"user {unindexed.id}: profile version 3 has no test-embedder query vectors yet" in caplog.text


async def test_hybrid_blends_stale_rescores_with_the_new_jobs(maker, hybrid):
    async with maker() as db:
        u = await backend_user(db)
        jobs = await title_vs_content_jobs(db)
        await service.match_user(db, FakeScoringAI(), u.id, budget=1)  # the blend's best, at version 3
        first = (await reviews(db))[0].job_id
        p = await db.get(Profile, u.id)
        p.version = 4
        await db.commit()
        await index_profile(db, u.id)
        ai = FakeScoringAI()
        await service.match_user(db, ai, u.id, budget=3)
        new = [r for r in await reviews(db) if r.profile_version == 4]
        # the stale re-score keeps its place by the blend: still first, with its cosine on the label
        assert new[0].job_id == first and new[0].retrieval_rank == 0 and new[0].retrieval_score is not None
        assert {r.job_id for r in new} == {j.id for j in jobs} and all(r.engine == "hybrid" for r in new)


def test_rank_work_with_the_hybrid_engine_orders_tier_two_by_the_blend():
    stale = [_c(1, 10, 5, match_id=101), _c(2, 90, 5, match_id=102), _c(3, 5, 1, match_id=103, saved=True),
             _c(4, 50, 9, match_id=104), _c(5, 1, 1, match_id=105, saved=True)]
    new = [_c(6, 1, 1), _c(7, 99, 10), _c(8, 50, 0)]
    for c, b in zip(stale + new, (0.9, -1.0, 2.0, 0.5, 0.0, 1.2, -0.3, 0.7), strict=True):
        c.blend = b
    order = [w.job_id for w in service.rank_work(stale, new, answered={105, 101}, engine="hybrid")]
    # answered by stored score (1 before 5), saved (3), then tier 2 by blend: 6 (1.2), 8 (0.7), 4 (0.5), 7, 2
    assert order == [1, 5, 3, 6, 8, 4, 7, 2]
