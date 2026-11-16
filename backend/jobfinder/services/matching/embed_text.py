"""Reference text builders for bge-small-jobfinder-v2. Copy this file into jobfinder as-is.

The model only means what it meant in training if its input text is built exactly the same way. These functions are
copied verbatim from notebooks/03_build_datasets.ipynb (cell 2), which built the training data. Don't "clean them up"
without retraining. `docs/golden_examples.json` pins their output, and `check_golden()` below tests against it.

Known quirk, kept on purpose: `evidence_items` uses `.strip(" at")`, which strips the *characters* ' ', 'a', 't' from both
ends, so "Analyst at Meta" becomes "Analyst at Me". v2 was trained with it. Fix it in 03 and here together at the next retrain.
"""
import re

JOB_DOC_MAX_CHARS = 1500  # title + company + the start of the description
JOB_INTRO_CHARS = 200  # how much company intro to keep ahead of the role section

# Prepended to the text before tokenizing. Postings and evidence get no prompt.
PROMPTS = {
    "profile_query": "Represent this job seeker's target role for finding matching job postings: ",
    "requirement": "Represent this job requirement for finding resume evidence that satisfies it: ",
    "posting": "",
    "evidence": "",
}

# Requirements matching this go to jobfinder's rule checks, never to the embedding (and sentences matching it are
# stripped from embedded text).
_CONSTRAINT = re.compile(
    r"\$\s?\d|\b\d{2,3}\s?k\b|\b(?:salary|compensation|pay range|base pay|equity|bonus|benefits?)\b"
    r"|\b(?:visa|sponsor\w*|work authori[sz]ation|authori[sz]ed to work|citizen\w*|green card|clearance)\b"
    r"|\b(?:relocat\w*|hybrid|on-?site|in-?office|remote|work from home|office|based in|located in)\b"
    r"|\b(?:graduat\w*|class of|bachelor|master|ph\.?d|degree)\b"
    r"|\b\d{1,2}\s*(?:\+|plus)?\s*(?:-|–|to)?\s*(?:\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)
_YEARS = re.compile(
    r"\(?\b\d{1,2}\s*(?:\+|plus)?\s*(?:-|–|to)?\s*(?:\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b(?:\s+of)?\)?\s*",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_WS = re.compile(r"\s+")
# First heading that starts the actual role.
_ROLE = re.compile(
    r"(?im)^[ \t]*(?:#+[ \t]*)?(?:what you(?:'|’)ll do|what you will do|(?:key )?responsibilities|the role|about the role"
    r"|about this role|your role|in this role|what you(?:'|’)ll be doing|job description|role overview|position overview"
    r"|overview of the role|the opportunity|what we(?:'|’)re looking for|requirements|qualifications|who you are|about you)"
    r"\b[^\n]{0,60}$"
)


def strip_constraints(text: str) -> str:
    """Drop every sentence/line that talks about pay, visa, place, degree or years."""
    parts = [p.strip() for p in _SENTENCE.split(text or "")]
    return " ".join(p for p in parts if p and not _CONSTRAINT.search(p))


def strip_years(text: str) -> str:
    """Remove the numeric experience threshold but keep the skill: '3+ years of REST APIs' -> 'REST APIs'."""
    return _WS.sub(" ", _YEARS.sub("", text or "")).strip(" -–,;:")


def _clean(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def is_rule_requirement(raw_requirement: str) -> bool:
    """True if a requirement belongs to the rule checks (pay, visa, place, degree, years), not the embedding judge."""
    return bool(_CONSTRAINT.search(raw_requirement or ""))


def job_doc(job: dict) -> str:
    """One vector per posting: title + company + a short intro + the role section onward (all constraint-free).

    job: {"title", "company_name", "description_text"}"""
    head = f"{_clean(job.get('title'))} at {_clean(job.get('company_name'))}".strip()
    text = job.get("description_text") or ""
    m = _ROLE.search(text)
    if m and m.start() > JOB_INTRO_CHARS:
        intro = strip_constraints(text[: m.start()])[:JOB_INTRO_CHARS].rsplit(" ", 1)[0]
        body = f"{intro} … {strip_constraints(text[m.start():])}"
    else:
        body = strip_constraints(text)
    return f"{head}. {body}"[:JOB_DOC_MAX_CHARS].strip()


def profile_skills(background: dict, limit: int = 8) -> list[str]:
    names = [s.get("name", "") for s in background.get("skills") or []]
    for e in background.get("experience") or []:
        names += e.get("technologies") or []
    for p in background.get("projects") or []:
        names += p.get("technologies") or []
    seen: dict[str, None] = {}
    for n in names:
        if n and n.lower() not in {k.lower() for k in seen}:
            seen[n] = None
    return list(seen)[:limit]


def profile_queries(profile: dict) -> list[str]:
    """One query per target role ('Role. Skills: a, b, c') plus the summary. Preferences are ignored.

    profile: {"status": {"target_roles": [...]}, "background": {"summary", "skills", "experience", "projects", ...}}"""
    status, background = profile.get("status") or {}, profile.get("background") or {}
    skills = ", ".join(profile_skills(background))
    queries = [f"{r}. Skills: {skills}" if skills else r for r in status.get("target_roles") or []]
    if background.get("summary"):
        queries.append(_clean(strip_constraints(background["summary"])))
    return [q for q in queries if q]


def evidence_items(profile: dict, facts: list[dict] | None = None) -> list[str]:
    """Every resume line a requirement could be satisfied by. Years and dates are left out.

    facts: answered profile questions [{"question", "answer"}] (profile_facts)."""
    background = profile.get("background") or {}
    items = [s["name"] for s in background.get("skills") or [] if s.get("name")]
    for e in background.get("experience") or []:
        head = f"{e.get('title', '')} at {e.get('company', '')}".strip(" at")  # quirk kept on purpose, see module doc
        if e.get("summary"):
            items.append(f"{head}: {_clean(e['summary'])}" if head else _clean(e["summary"]))
        items += [strip_years(b) for b in e.get("bullets") or []]
    for p in background.get("projects") or []:
        if p.get("description"):
            items.append(f"{p.get('name', '')}: {_clean(p['description'])}".strip(": "))
    items += [f"{f['question']} {f['answer']}".strip() for f in facts or [] if f.get("answer")]
    items += list(background.get("certifications") or [])
    return [i for i in (_clean(i) for i in items) if i]


def requirement_text(requirement: str) -> str:
    return strip_years(_clean(requirement))


def check_golden(path: str = "golden_examples.json", embed=None) -> None:
    """Assert these builders (and, if given, `embed(texts, prompt) -> unit vectors`) reproduce the golden file."""
    import json
    from pathlib import Path

    g = json.loads(Path(path).read_text(encoding="utf-8"))
    assert job_doc(g["job"]) == g["expected"]["job_doc"]
    assert profile_queries(g["profile"]) == g["expected"]["profile_queries"]
    assert evidence_items(g["profile"], g["facts"]) == g["expected"]["evidence_items"]
    assert [requirement_text(r) for r in g["requirements"]] == g["expected"]["requirement_texts"]
    assert [is_rule_requirement(r) for r in g["requirements"]] == g["expected"]["is_rule_requirement"]
    if embed is not None:
        import numpy as np

        req = embed(g["expected"]["requirement_texts"], PROMPTS["requirement"])
        ev = embed(g["expected"]["evidence_items"], PROMPTS["evidence"])
        got = (req @ ev.T).max(axis=1)
        assert np.allclose(got, g["expected"]["requirement_max_cos"], atol=1e-3), got
