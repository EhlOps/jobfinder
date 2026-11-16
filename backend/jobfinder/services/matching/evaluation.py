"""`jobfinder ops eval-retrieval` (#126): how well each retrieval engine's top N agrees with Claude, read-only.

For every user with Claude-scored matches whose current profile version has query vectors, it ranks the jobs by
both engines as `retrieve` would, except that already-matched jobs stay in (they are the labelled ones), and
compares the two top-N lists against `job_matches` scored at that version (older scores judged another
profile). Postings are compared by dedupe_hash, the unit both engines keep one row of.
"""
from __future__ import annotations

from statistics import mean

from jobfinder.core.config import settings
from jobfinder.core.db import AsyncSession
from jobfinder.models import Profile
from jobfinder.repositories import retrieval as retrieval_repo
from jobfinder.services.matching.service import (
    PREFILTER,
    VECTOR,
    Candidate,
    prefilter_candidates,
    vector_candidates,
)

TOP = 25  # what one run of the vector engine is for: a profile's closest new jobs
BEST = 10  # the user's best-scored jobs whose rank each engine reports
GOOD = ("good", "strong")
ENGINES = (PREFILTER, VECTOR)


def _engine_report(ranked: list[Candidate], labels: dict[str, tuple[int, str]], best: list[str], top: int) -> dict:
    head = [c.dedupe_hash for c in ranked[:top]]
    scored = [labels[h] for h in head if h in labels]
    position = {c.dedupe_hash: i + 1 for i, c in enumerate(ranked)}
    return {
        "candidates": len(ranked),
        "scored": len(scored),
        "mean_llm": round(mean(s for s, _ in scored), 1) if scored else None,
        "good_share": round(sum(v in GOOD for _, v in scored) / len(scored), 2) if scored else None,
        "best_ranks": [position.get(h) for h in best],  # 1-based; None: not retrieved (filtered, or past K)
    }


async def evaluate_retrieval(db: AsyncSession, *, top: int = TOP) -> dict:
    """{"model", "top", "users": [per-user report], "skipped": [user ids without a profile or query vectors]}."""
    users, skipped = [], []
    for user_id in await retrieval_repo.users_with_scored_matches(db):
        profile = await db.get(Profile, user_id)
        if (profile is None or not profile.status
                or not await retrieval_repo.has_query_vectors(db, user_id, profile.version, settings.embed_model)):
            skipped.append(user_id)
            continue
        labels: dict[str, tuple[int, str]] = {}
        for row in await retrieval_repo.scored_postings(db, user_id, profile.version):  # best first: a posting keeps its best score
            labels.setdefault(row.dedupe_hash, (row.llm_score, row.verdict))
        best = list(labels)[:BEST]
        ranked = {
            PREFILTER: await prefilter_candidates(db, user_id, profile, include_matched=True),
            VECTOR: await vector_candidates(db, user_id, profile, include_matched=True),
        }
        heads = [{c.dedupe_hash for c in r[:top]} for r in ranked.values()]
        users.append({
            "user_id": user_id, "profile_version": profile.version, "labelled": len(labels),
            "overlap": len(heads[0] & heads[1]),
            **{engine: _engine_report(r, labels, best, top) for engine, r in ranked.items()},
        })
    return {"model": settings.embed_model, "top": top, "users": users, "skipped": skipped}
