"""ATS keyword coverage: which terms from a posting does the resume actually contain?

Pure functions, no database or network. `extract_terms` pulls skill-like keywords out of a posting
(a curated vocabulary plus anything that looks like a technology name: `C++`, `CI/CD`, `Node.js`,
`TypeScript`, `AWS`); `coverage` checks them against resume text the way the posting's ATS is
likely to. Matching is heuristic: it tells the candidate what to double-check, not what a given
recruiter's search will do."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

# ── per-ATS notes ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AtsProfile:
    name: str
    # literal: a term only counts when it appears as written (case-insensitive). Plural / alias /
    # punctuation variants (`Node.js` vs `node`) are reported as `loose` instead of covered.
    literal: bool
    notes: tuple[str, ...]


ATS_PROFILES: dict[str, AtsProfile] = {
    "greenhouse": AtsProfile("greenhouse", True, (
        "Recruiters mostly search the parsed resume text for literal keywords, so use the posting's exact spelling.",
        "Upload a text-based PDF or DOCX; tables, columns and text boxes can scramble the parsed order.",
        "Spell out acronyms once, e.g. 'Continuous Integration (CI/CD)'.",
    )),
    "lever": AtsProfile("lever", True, (
        "Lever parses the resume into a profile and supports keyword search over it, so match the posting's wording.",
        "Put skills in plain text under a clear 'Skills' or 'Experience' heading; avoid headers and footers.",
        "Standard section titles parse best.",
    )),
    "ashby": AtsProfile("ashby", False, (
        "Ashby's search is more tolerant of variants, so close synonyms and plurals usually still match.",
        "Still keep the key terms from the posting visible in your experience bullets, not only in a skills list.",
        "Single-column, text-based PDFs parse most reliably.",
    )),
}

DEFAULT_PROFILE = AtsProfile("default", False, (
    "ATS not recognised: assuming a keyword search that tolerates case, plurals and punctuation variants.",
    "Use a single-column, text-based resume with standard section headings.",
    "Mirror the posting's wording for the skills you really have.",
))


def ats_profile(ats: str | None) -> AtsProfile:
    return ATS_PROFILES.get((ats or "").strip().lower(), DEFAULT_PROFILE)



# ── normalisation ────────────────────────────────────────────────────────
# A token starts alphanumeric (or a leading dot, as in `.NET`) and may contain + # . / - inside.
_TOKEN = re.compile(r"\.?[A-Za-z0-9][A-Za-z0-9+#./\-]*")
_ALIASES = {
    "golang": "go", "nodejs": "node", "reactjs": "react", "vuejs": "vue", "nextjs": "next.js",
    "k8s": "kubernetes", "postgres": "postgresql", "js": "javascript", "ts": "typescript",
    "cicd": "ci/cd", "dotnet": ".net",
}

# Terms worth matching that the shape heuristics below would miss (lower-case words or phrases).
VOCAB = [
    "python", "java", "javascript", "typescript", "go", "rust", "ruby", "php", "kotlin", "swift", "scala",
    "sql", "html", "css", "bash", "react", "vue", "angular", "svelte", "node", "django", "flask", "fastapi",
    "rails", "spring", "graphql", "rest", "grpc", "aws", "azure", "gcp", "docker", "kubernetes", "terraform",
    "ansible", "jenkins", "git", "linux", "devops", "sre", "postgresql", "mysql", "mongodb", "redis",
    "kafka", "spark", "airflow", "snowflake", "elasticsearch", "tensorflow", "pytorch", "pandas", "numpy",
    "microservices", "agile", "scrum", "ci/cd",
    "machine learning", "deep learning", "data science", "data engineering", "cloud security",
]
# Also ordinary English words: only counted when written capitalised ("Go", "REST", "Spark").
AMBIGUOUS = frozenset({"go", "rest", "spring", "swift", "spark", "scala"})
# Shaped like a technology name but not a skill.
NOT_SKILLS = frozenset({
    "us", "usa", "uk", "eu", "eeo", "pto", "hr", "nyc", "ceo", "cto", "cfo", "coo", "vp", "faq", "llc", "inc",
    "linkedin", "paypal", "youtube", "ebay", "iphone",
})


def _clean(token: str) -> str:
    # Trailing sentence punctuation is not part of the term; `+` and `#` are (C++, C#).
    return token.rstrip(".-/")


def canon(term: str) -> str:
    """Fold case, hyphens, a trailing `.js` and simple plurals so variants compare equal."""
    out = []
    for word in term.lower().split():
        t = _clean(word).replace("-", "")
        base = re.sub(r"\.js$", "", t)
        t = _ALIASES.get(base, base) if base != "next" else t  # "Next.js" must not equal the word "next"
        if t.endswith("ies") and len(t) > 4:
            t = t[:-3] + "y"
        elif t.endswith(("sses", "xes", "ches", "shes")):
            t = t[:-2]
        elif t.endswith("s") and len(t) > 3 and not t.endswith(("ss", "us", "is")):
            t = t[:-1]
        out.append(t)
    return " ".join(out)


def _variants(text: str, fold) -> str:
    """Tokenise three ways so any form of a term can match: as written, hyphens split
    (machine-learning), and slashes split (AWS/GCP). `fold` normalises each token."""
    parts = [
        " ".join(fold(t) for t in _TOKEN.findall(v))
        for v in (text, text.replace("-", " "), text.replace("/", " "))
    ]
    return " " + " | ".join(parts) + " "


def _canon_text(text: str) -> str:
    return _variants(text, canon)


def _literal_text(text: str) -> str:
    return _variants(text, lambda t: _clean(t.lower()))


# ── term extraction ──────────────────────────────────────────────────────
_VOCAB_CANON = {canon(v): v for v in VOCAB}
_SHAPED = re.compile(r"[A-Za-z][+#]|(?i:\.js)$|^\.[A-Za-z]")  # C++, C#, Node.js, .NET
_ACRONYM = re.compile(r"[A-Z]{2,5}")  # AWS, SQL, ETL
_CAMEL = re.compile(r"[A-Z][a-z]+[A-Z][A-Za-z]*|[a-z]+[A-Z][A-Za-z]*")  # TypeScript, GitHub


def _candidates(tok: str):
    """The token itself, or its slash-separated parts ('AWS/GCP') unless it is a known term."""
    if "/" in tok and canon(tok) not in _VOCAB_CANON and not _SHAPED.search(tok):
        yield from (p for p in tok.split("/") if p)
    else:
        yield tok


def extract_terms(posting_text: str, limit: int = 40) -> list[str]:
    """Skill-like terms from a posting, most frequent first (ties keep first-seen order)."""
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}

    def add(term: str, n: int = 1) -> None:
        key = canon(term)
        counts[key] += n
        display.setdefault(key, term)

    for line in posting_text.splitlines():
        letters = [c for c in line if c.isalpha()]
        if letters and all(c.isupper() for c in letters):  # a heading like "WHAT YOU NEED"
            continue
        for raw in _TOKEN.findall(line):
            for tok in _candidates(_clean(raw)):
                key = canon(tok)
                if key in NOT_SKILLS:
                    continue
                if key in _VOCAB_CANON:
                    if key in AMBIGUOUS and not tok[0].isupper():
                        continue
                    add(_VOCAB_CANON[key] if tok.islower() else tok)
                elif _SHAPED.search(tok) or _ACRONYM.fullmatch(tok) or _CAMEL.fullmatch(tok):
                    add(tok)
    text = " " + " ".join(canon(t) for t in _TOKEN.findall(posting_text.replace("-", " "))) + " "
    for key, phrase in _VOCAB_CANON.items():
        if " " in key and (n := text.count(f" {key} ")):
            add(phrase, n)
    return [display[k] for k, _ in counts.most_common(limit)]


# ── coverage ─────────────────────────────────────────────────────────────
@dataclass
class Coverage:
    ats: str
    covered: list[str]
    missing: list[str]
    loose: list[str]  # present only in another spelling/plural; missing for literal-matching ATSes
    percent: int
    notes: list[str]


def coverage(resume_text: str, terms: list[str], ats: str | None = None) -> Coverage:
    profile = ats_profile(ats)
    folded = _canon_text(resume_text)
    literal = _literal_text(resume_text)
    covered: list[str] = []
    missing: list[str] = []
    loose: list[str] = []
    for term in terms:
        in_folded = f" {canon(term)} " in folded
        in_literal = f" {_clean(term.lower())} " in literal
        if in_literal or (in_folded and not profile.literal):
            covered.append(term)
        else:
            missing.append(term)
            if in_folded:
                loose.append(term)
    percent = round(100 * len(covered) / len(terms)) if terms else 100
    return Coverage(profile.name, covered, missing, loose, percent, list(profile.notes))
