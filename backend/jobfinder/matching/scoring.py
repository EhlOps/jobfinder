"""The stored match score is computed here from the model's per-requirement judgements, not taken from
the model's own gut number. 'Unknown' (the profile is silent) is neutral, so a thin profile scores
mid-range with low confidence instead of looking like a poor fit; 'unmet' is what lowers the score."""
from __future__ import annotations

from jobfinder.ai.schemas import MatchScore, RequirementCheck

IMPORTANCE_WEIGHT = {"must": 3.0, "nice": 1.0}
STATUS_CREDIT = {"met": 1.0, "partial": 0.6, "unknown": 0.5, "unmet": 0.0}
UNMET_MUST_CAP = 64  # a missing must-have can never make a "good" (65+) match
QUALIFICATION_SHARE = 0.8  # remaining 20% is fit with the stated preferences


def compute_score(reqs: list[RequirementCheck], preference_fit: int) -> tuple[int, float]:
    """-> (score 0-100, confidence 0-1). With no requirements to go on, returns a neutral 50 / 0.3."""
    if not reqs:
        return 50, 0.3
    total = sum(IMPORTANCE_WEIGHT[r.importance] for r in reqs)
    qual = sum(IMPORTANCE_WEIGHT[r.importance] * STATUS_CREDIT[r.status] for r in reqs) / total
    score = round(QUALIFICATION_SHARE * qual * 100 + (1 - QUALIFICATION_SHARE) * preference_fit)
    if any(r.importance == "must" and r.status == "unmet" for r in reqs):
        score = min(score, UNMET_MUST_CAP)
    unknown = sum(IMPORTANCE_WEIGHT[r.importance] for r in reqs if r.status == "unknown") / total
    return max(0, min(100, score)), round(1 - unknown, 2)


def finalize(result: MatchScore) -> MatchScore:
    """Replace the model's gut score/confidence with the computed ones and derive `unknowns` and `gaps`
    from the requirement checks (kept in the old shape for the email and match list)."""
    from jobfinder.ai.schemas import Unknown

    if not result.requirements:  # the model gave no checklist: keep its own judgement
        return result
    score, confidence = compute_score(result.requirements, result.preference_fit)
    asks = [r for r in result.requirements if r.question and r.status != "met"]
    asks.sort(key=lambda r: (r.importance != "must", r.status == "unmet"))
    unknowns = [Unknown(question=r.question, why=f"The posting asks for: {r.requirement}") for r in asks]
    gaps = result.gaps or [r.requirement for r in result.requirements if r.status in ("unmet", "partial") and r.importance == "must"]
    return result.model_copy(update={"score": score, "confidence": confidence, "unknowns": unknowns, "gaps": gaps[:4]})
