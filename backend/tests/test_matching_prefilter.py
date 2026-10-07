from datetime import UTC, datetime, timedelta

import pytest

from jobfinder.matching import prefilter as pf


# ── seniority ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("status", "expected"), [
    ({}, None),
    ({"is_new_grad": True}, {"new_grad", "junior"}),
    ({"seniority": ["intern"]}, {"intern"}),
    ({"seniority": ["new_grad", "intern"]}, {"new_grad", "junior", "intern"}),
    ({"seniority": ["senior"]}, {"mid", "senior", "staff"}),
    ({"is_new_grad": True, "seniority": ["mid"]}, {"junior", "mid", "senior"}),  # explicit choice wins
])
def test_acceptable_seniorities(status, expected):
    assert pf.acceptable_seniorities(status) == expected


def test_unknown_level_always_acceptable():
    ok = {"new_grad", "junior"}
    assert pf.seniority_ok(None, ok) and pf.seniority_ok("junior", ok)
    assert not pf.seniority_ok("senior", ok) and not pf.seniority_ok("manager", ok)
    assert pf.seniority_ok("staff", None)


# ── location ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("loc", "target", "ok"), [
    ("Boston, MA", "Boston", True),
    ("Seattle; San Francisco; New York City", "NYC", True),
    ("San Francisco Bay Area", "SF", True),
    ("Boston, MA", "United States", True),
    ("Remote - US", "USA", True),
    ("London, UK", "United States", False),
    ("Dublin", "Boston", False),
    ("New York, NY (HQ)", "New York", True),
    ("Austin, TX", "", False),
])
def test_location_matches(loc, target, ok):
    assert pf.location_matches(loc, target) is ok


def test_place_prefs_variants():
    p = pf.place_prefs({"target_locations": ["Boston", "Remote"], "remote_preference": None})
    assert p.targets == ["Boston"] and p.accepts_remote and not p.remote_only and not p.anywhere
    p = pf.place_prefs({"remote_preference": "remote"})
    assert p.remote_only and p.accepts_remote
    p = pf.place_prefs({"target_locations": ["Remote"]})
    assert p.remote_only and p.targets == []
    p = pf.place_prefs({"target_locations": ["Boston"], "willing_to_relocate": True})
    assert p.anywhere
    assert pf.place_prefs({}).anywhere  # nothing specified: no constraint


def test_location_ok_rules():
    boston = pf.place_prefs({"target_locations": ["Boston"], "remote_preference": "onsite"})
    assert pf.location_ok("Boston, MA", None, boston)
    assert not pf.location_ok("Dublin", "hybrid", boston)
    assert not pf.location_ok("Anywhere", "remote", boston)           # onsite-only candidate: remote role doesn't fit
    assert pf.location_ok("", None, boston)                             # unknown location is kept
    remote = pf.place_prefs({"remote_preference": "remote"})
    assert pf.location_ok("Remote - US", "remote", remote) and not pf.location_ok("Boston, MA", "hybrid", remote)
    assert not pf.location_ok("Boston, MA", None, remote)               # unknown workplace isn't assumed remote
    mixed = pf.place_prefs({"target_locations": ["Boston", "Remote"]})
    assert pf.location_ok("Remote", "remote", mixed) and pf.location_ok("Boston", None, mixed)


# ── salary / sponsorship ─────────────────────────────────────────────────
def test_salary_ok():
    s = {"salary_min": 120000}
    assert pf.salary_ok(s, 150000, "USD") and pf.salary_ok(s, None, None) and pf.salary_ok(s, 80000, "EUR")
    assert not pf.salary_ok(s, 100000, "USD") and pf.salary_ok({}, 1, "USD")


@pytest.mark.parametrize(("text", "refuses"), [
    ("We are unable to provide visa sponsorship for this role.", True),
    ("Applicants must be authorized to work in the US without sponsorship.", True),
    ("No visa sponsorship available", True),
    ("Must be a U.S. citizen", True),
    ("Active TS/SCI clearance required", True),
    ("We will not sponsor work authorization", True),
    ("We offer visa sponsorship for qualified candidates!", False),
    ("Great team, build things", False),
    ("", False),
])
def test_refuses_sponsorship(text, refuses):
    assert pf.refuses_sponsorship(text) is refuses


# ── scoring ──────────────────────────────────────────────────────────────
BG = {"skills": [{"name": "Python"}, {"name": "Go"}, {"name": "C++"}],
      "experience": [{"technologies": ["Postgres", "Kafka"]}], "projects": [{"technologies": ["React"]}]}


def test_profile_skills_deduped_lowercase():
    assert pf.profile_skills(BG) == ["python", "go", "c++", "postgres", "kafka", "react"]
    assert pf.profile_skills({}) == []


def test_skill_fraction_uses_word_boundaries():
    skills = ["go", "c++", "python"]
    assert pf.skill_fraction(skills, "Engineer", "We use Python and C++ daily") > pf.skill_fraction(skills, "Engineer", "We use gopher tooling and cpp")
    assert pf.skill_fraction(["go"], "Engineer", "gopher google") == 0
    assert pf.skill_fraction([], "x", "y") == 0.5


def test_title_similarity():
    roles = ["Backend Engineer"]
    assert pf.title_similarity("Backend Engineer", roles) == 1.0
    assert pf.title_similarity("Software Engineer, Payments", roles) == 0.5
    assert pf.title_similarity("Account Executive", roles) == 0
    assert pf.title_similarity("Anything", []) == 0.5


def test_recency_and_prestige():
    now = datetime.now(UTC)
    assert pf.recency(now - timedelta(days=2), now) == 1.0
    assert 0 < pf.recency(now - timedelta(days=30), now) < 1
    assert pf.recency(now - timedelta(days=90), now) == 0 and pf.recency(None) == 0.3
    assert pf.prestige_fit(5, 5) == 1.0 and pf.prestige_fit(2, 5) == 0.4
    assert pf.prestige_fit(2, None) == 0.5 and pf.prestige_fit(None, 5) == 0.5 and pf.prestige_fit(1, 2) == 0.5


def _score(**kw):
    base = {
        "title": "Backend Engineer", "description": "Python Postgres Kafka services", "posted_at": datetime.now(UTC),
        "company_tier": 4, "company_size": "large", "company_industry": "fintech",
        "status": {"target_roles": ["Backend Engineer"], "prestige_preference": 4}, "skills": pf.profile_skills(BG),
    }
    return pf.prefilter_score(**{**base, **kw})


def test_prefilter_ranks_relevant_jobs_higher():
    good = _score()
    assert good > _score(title="Account Executive", description="Sell things") + 40
    assert good > _score(description="nothing relevant here")
    assert 0 <= _score(title="zzz", description="") <= 100 and good <= 100


def test_prefilter_size_and_industry_adjustments():
    status = {"target_roles": ["Backend Engineer"], "company_sizes": ["startup"], "industries": ["fintech"]}
    wrong_size = _score(status=status, company_size="large", company_industry="travel")
    right = _score(status=status, company_size="startup", company_industry="Fintech")
    assert right - wrong_size == pytest.approx(10)


# ── required experience ──────────────────────────────────────────────────
@pytest.mark.parametrize(("text", "years"), [
    ("5+ years of experience in backend development", 5),
    ("3-5 years of professional software engineering experience", 3),
    ("at least 4 years' experience with distributed systems", 4),
    ("We have been around for 10 years, come join us", None),          # not about the candidate
    ("1+ years of experience with Python", 1),
    ("2 years experience required, ideally 7+ years of industry experience", 7),
    ("Bachelor's degree or equivalent practical experience", None),
    ("", None),
])
def test_required_years(text, years):
    assert pf.required_years(text) == years


def test_too_experienced_only_filters_entry_level_candidates():
    entry = {"new_grad", "junior"}
    assert pf.too_experienced("5+ years of experience", entry)
    assert pf.too_experienced("3 years of experience", entry)
    assert not pf.too_experienced("2 years of experience", entry)
    assert not pf.too_experienced("5+ years of experience", {"senior", "staff"})
    assert not pf.too_experienced("5+ years of experience", None)
    assert not pf.too_experienced("5+ years of experience", {"intern", "new_grad", "mid"})   # chose a higher level too
    assert not pf.too_experienced("no mention", entry)
