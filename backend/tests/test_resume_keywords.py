from pathlib import Path

from jobfinder.resumes.keywords import DEFAULT_PROFILE, ats_profile, canon, coverage, extract_terms

FIX = Path(__file__).parent / "fixtures"


def lower(terms):
    return {t.lower() for t in terms}


def test_extract_terms_from_real_posting():
    terms = lower(extract_terms((FIX / "posting_ramp_security.txt").read_text()))
    assert {"aws", "terraform", "devops"} <= terms
    # headings and filler are not skills
    assert not terms & {"about", "ramp", "what", "need", "you"}


def test_extract_terms_special_tokens():
    text = "You know C++, C#, CI/CD and Node.js. Experience with TypeScript and GitHub is a plus."
    assert lower(extract_terms(text)) >= {"c++", "c#", "ci/cd", "node.js", "typescript", "github"}


def test_extract_terms_ranks_by_frequency_and_dedupes_variants():
    terms = extract_terms("We use Docker. Docker containers! Kubernetes and docker; Python, python.", limit=2)
    assert terms == ["Docker", "Python"]
    assert len(extract_terms("kubernetes Kubernetes k8s")) == 1


def test_extract_terms_ignores_shouting_headings():
    assert extract_terms("WHAT YOU NEED\nABOUT THE ROLE\nAWS experience") == ["AWS"]


def test_extract_terms_skips_noise():
    text = "See engineering.ramp.com, $150,000+ in the US, and/or 24/7 as the CEO says. The rest of the team will go far."
    assert extract_terms(text) == []
    assert lower(extract_terms("Strong in Go and AWS/GCP; REST APIs.")) == {"go", "aws", "gcp", "rest"}


def test_multiword_terms():
    assert "machine learning" in lower(extract_terms("Experience with machine-learning pipelines."))


def test_canon_folds_case_plural_punctuation():
    assert canon("Node.js") == canon("node") == canon("NodeJS")
    assert canon("Containers") == canon("container")
    assert canon("front-end") == canon("frontend")
    assert canon("C++") == "c++" and canon("C++") != canon("C")
    assert canon("CI/CD") == "ci/cd"


def test_coverage_missing_and_percent():
    cov = coverage("Built services in Python and C++ with CI/CD on AWS.", ["Python", "C++", "CI/CD", "Terraform"], "ashby")
    assert cov.covered == ["Python", "C++", "CI/CD"]
    assert cov.missing == ["Terraform"]
    assert cov.percent == 75


def test_coverage_punctuation_in_resume():
    cov = coverage("Skills: python, aws; (terraform).", ["Python", "AWS", "Terraform"], None)
    assert cov.missing == [] and cov.percent == 100


def test_coverage_slash_joined_resume_skills():
    cov = coverage("Used AWS/GCP and Python/Django, Kubernetes.", ["AWS", "GCP", "Python", "Django"], "greenhouse")
    assert cov.percent == 100


def test_coverage_es_plurals_and_next_js():
    assert coverage("Ran many processes", ["process"], "ashby").percent == 100
    assert coverage("Planned next steps", ["Next.js"], "ashby").missing == ["Next.js"]


def test_coverage_c_does_not_match_cpp():
    assert coverage("I know C++ well.", ["C"], "ashby").missing == ["C"]


def test_literal_ats_flags_variants_but_lenient_ats_accepts_them():
    resume = "Five years of node and REST APIs."
    gh = coverage(resume, ["Node.js"], "greenhouse")
    assert gh.missing == ["Node.js"] and gh.loose == ["Node.js"] and gh.percent == 0
    ash = coverage(resume, ["Node.js"], "ashby")
    assert ash.covered == ["Node.js"] and ash.percent == 100


def test_exact_spelling_covers_on_every_ats():
    for ats in ("greenhouse", "lever", "ashby", "other"):
        assert coverage("Used Node.js daily", ["Node.js"], ats).percent == 100


def test_plural_variant_on_literal_ats():
    assert coverage("Wrote microservice code", ["microservices"], "lever").loose == ["microservices"]


def test_unknown_ats_falls_back_to_default():
    for value in (None, "", "workday"):
        assert ats_profile(value) is DEFAULT_PROFILE
    assert ats_profile("GREENHOUSE ").name == "greenhouse"
    cov = coverage("python", ["Python"], "workday")
    assert cov.ats == "default" and cov.notes


def test_each_ats_has_notes():
    for ats in ("greenhouse", "lever", "ashby", None):
        assert coverage("", ["x"], ats).notes


def test_no_terms_is_fully_covered():
    assert coverage("anything", [], "lever").percent == 100
