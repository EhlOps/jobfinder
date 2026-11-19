"""`jobfinder ops eval-retrieval` (#126): how well each retrieval engine's top N agrees with Claude, read-only.

For every user with Claude-scored matches whose current profile version has query vectors, it ranks the jobs by
each engine as `retrieve` would, except that already-matched jobs stay in (they are the labelled ones), and
compares the top-N lists against `job_matches` scored at that version (older scores judged another profile;
`labels_version` picks one anyway, for a profile edited since its matches were scored). Postings are compared
by dedupe_hash, the unit every engine keeps one row of.
"""
from __future__ import annotations

from statistics import mean

from jobfinder.core.config import settings
from jobfinder.core.db import AsyncSession
from jobfinder.models import Profile
from jobfinder.repositories import retrieval as retrieval_repo
from jobfinder.services.matching.service import (
    HYBRID,
    PREFILTER,
    VECTOR,
    Candidate,
    hybrid_candidates,
    prefilter_candidates,
    vector_candidates,
)

TOP = 25  # what one run of the vector engine is for: a profile's closest new jobs
BEST = 10  # the user's best-scored jobs whose rank each engine reports
GOOD = ("good", "strong")
ENGINES = (PREFILTER, VECTOR, HYBRID)


def _engine_report(ranked: list[Candidate], labels: dict[str, tuple[int, str]], best: list[str], top: int,
                   baseline: list[Candidate]) -> dict:
    head = [c.dedupe_hash for c in ranked[:top]]
    scored = [labels[h] for h in head if h in labels]
    position = {c.dedupe_hash: i + 1 for i, c in enumerate(ranked)}
    return {
        "candidates": len(ranked),
        "scored": len(scored),
        "mean_llm": round(mean(s for s, _ in scored), 1) if scored else None,
        "good_share": round(sum(v in GOOD for _, v in scored) / len(scored), 2) if scored else None,
        "best_ranks": [position.get(h) for h in best],  # 1-based; None: not retrieved (filtered, or past K)
        "overlap": len(set(head) & {c.dedupe_hash for c in baseline[:top]}),  # with the prefilter's top N
    }


async def evaluate_retrieval(db: AsyncSession, *, top: int = TOP, labels_version: int | None = None) -> dict:
    """{"model", "top", "users": [per-user report], "skipped": [user ids without a profile or query vectors]}.
    `labels_version`: judge by the matches scored at this profile version instead of the current one."""
    users, skipped = [], []
    for user_id in await retrieval_repo.users_with_scored_matches(db):
        profile = await db.get(Profile, user_id)
        if (profile is None or not profile.status
                or not await retrieval_repo.has_query_vectors(db, user_id, profile.version, settings.embed_model)):
            skipped.append(user_id)
            continue
        labels: dict[str, tuple[int, str]] = {}
        version = profile.version if labels_version is None else labels_version
        for row in await retrieval_repo.scored_postings(db, user_id, version):  # best first: a posting keeps its best score
            labels.setdefault(row.dedupe_hash, (row.llm_score, row.verdict))
        best = list(labels)[:BEST]
        ranked = {
            PREFILTER: await prefilter_candidates(db, user_id, profile, include_matched=True),
            VECTOR: await vector_candidates(db, user_id, profile, include_matched=True),
            HYBRID: await hybrid_candidates(db, user_id, profile, include_matched=True),
        }
        users.append({
            "user_id": user_id, "profile_version": profile.version, "labels_version": version,
            "labelled": len(labels),
            **{engine: _engine_report(r, labels, best, top, ranked[PREFILTER]) for engine, r in ranked.items()},
        })
    return {"model": settings.embed_model, "top": top, "users": users, "skipped": skipped}
