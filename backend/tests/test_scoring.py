from jobfinder.ai.schemas import MatchScore, RequirementCheck
from jobfinder.matching.scoring import compute_score, finalize


def req(status, importance="must", question=""):
    return RequirementCheck(requirement=f"{importance} {status}", importance=importance, status=status, question=question)


def test_all_met_is_near_perfect():
    score, conf = compute_score([req("met"), req("met", "nice")], preference_fit=100)
    assert score == 100 and conf == 1.0


def test_all_unknown_is_neutral_with_low_confidence():
    score, conf = compute_score([req("unknown"), req("unknown")], preference_fit=70)
    assert score == 54 and conf == 0.0  # 0.8*50 + 0.2*70


def test_unmet_must_have_caps_below_good():
    score, _ = compute_score([req("met"), req("met"), req("met"), req("unmet")], preference_fit=100)
    assert score == 64


def test_unmet_nice_to_have_does_not_cap():
    score, _ = compute_score([req("met"), req("met"), req("unmet", "nice")], preference_fit=100)
    assert score > 64


def test_partial_sits_between_unknown_and_met():
    partial, _ = compute_score([req("partial")], 70)
    unknown, _ = compute_score([req("unknown")], 70)
    met, _ = compute_score([req("met")], 70)
    assert unknown < partial < met


def test_no_requirements_is_neutral():
    assert compute_score([], 70) == (50, 0.3)


def test_finalize_derives_unknowns_must_first_and_skips_met():
    out = finalize(MatchScore(score=99, confidence=0.9, requirements=[
        req("met", question="ignored"),
        req("unknown", "nice", question="nice q"),
        req("partial", "must", question="must q"),
    ]))
    assert [u.question for u in out.unknowns] == ["must q", "nice q"]
    assert out.score != 99


def test_finalize_without_requirements_keeps_model_score():
    assert finalize(MatchScore(score=77, confidence=0.8)).score == 77
