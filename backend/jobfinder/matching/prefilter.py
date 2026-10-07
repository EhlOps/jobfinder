"""Cheap, deterministic filtering and ranking that runs before any LLM call.

Hard filters drop jobs the candidate clearly can't or won't take (wrong level, below their salary
floor, needs sponsorship the job refuses, wrong place). A 0-100 prefilter score then ranks what's
left so the limited LLM budget goes to the most promising jobs first. Unknown data never excludes
a job: if a posting doesn't say where/what level/how much, it stays and the LLM judges it."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

# ── seniority ────────────────────────────────────────────────────────────
# Levels (as inferred from titles) acceptable for each level the candidate selects. Jobs whose
# level couldn't be inferred (None) are always acceptable.
ACCEPTABLE = {
    "intern": {"intern"},
    "new_grad": {"new_grad", "junior"},
    "junior": {"junior", "new_grad"},
    "mid": {"junior", "mid", "senior"},
    "senior": {"mid", "senior", "staff"},
    "staff": {"staff", "senior"},
    "manager": {"manager", "staff"},
}


def acceptable_seniorities(status: dict) -> set[str] | None:
    """None means 'don't filter on level'."""
    chosen = status.get("seniority") or []
    if not chosen and status.get("is_new_grad"):
        chosen = ["new_grad"]
    if not chosen:
        return None
    out: set[str] = set()
    for level in chosen:
        out |= ACCEPTABLE.get(level, {level})
    return out


def seniority_ok(job_level: str | None, acceptable: set[str] | None) -> bool:
    return acceptable is None or job_level is None or job_level in acceptable


# ── location ─────────────────────────────────────────────────────────────
_ALIASES = {"nyc": "new york", "new york city": "new york", "sf": "san francisco", "bay area": "san francisco",
            "la": "los angeles", "dc": "washington", "ny": "new york"}
_US_NAMES = {"united states", "usa", "us", "u.s.", "america", "united states of america"}
_US_STATES = frozenset(
    ["AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"]
)
_REMOTE_WORDS = {"remote", "anywhere", "work from home", "wfh"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9., ]+", " ", s.lower())).strip()


_US_STATE_RE = re.compile(r",\s*(?:" + "|".join(sorted(_US_STATES)) + r")\b")
_US_WORD_RE = re.compile(r"\b(?:united states|usa|u\.s\.)\b|\bus\b", re.IGNORECASE)


def _is_us(job_location: str) -> bool:
    """'Boston, MA', 'Remote - US', 'United States' all count as the US."""
    return bool(_US_WORD_RE.search(job_location) or _US_STATE_RE.search(job_location))


def location_matches(job_location: str, target: str) -> bool:
    """Does one of the job's locations ('A; B; C') satisfy this target place?"""
    t = _ALIASES.get(_norm(target), _norm(target))
    if not t:
        return False
    if t in _US_NAMES:
        return _is_us(job_location)
    low = _norm(job_location)
    for alias, full in _ALIASES.items():
        low = re.sub(rf"\b{re.escape(alias)}\b", full, low)
    return t in low


@dataclass
class Place:
    remote_only: bool  # candidate only wants remote roles
    accepts_remote: bool  # remote roles are fine
    targets: list[str]  # physical places (cities/regions/countries)
    anywhere: bool  # no real constraint (relocating, or nothing specified)


def place_prefs(status: dict) -> Place:
    raw = [t.strip() for t in status.get("target_locations") or [] if t and t.strip()]
    targets = [t for t in raw if _norm(t) not in _REMOTE_WORDS]
    wants_remote_word = len(targets) != len(raw)
    pref = status.get("remote_preference")
    remote_only = pref == "remote" or (wants_remote_word and not targets and pref in (None, "any", ""))
    accepts_remote = remote_only or wants_remote_word or pref in (None, "any", "", "hybrid", "remote")
    anywhere = status.get("willing_to_relocate") is True or (not targets and not remote_only)
    return Place(remote_only, accepts_remote, targets, anywhere)


def location_ok(job_location: str, workplace: str | None, place: Place) -> bool:
    if workplace == "remote":
        return place.accepts_remote or place.anywhere
    if place.remote_only:
        return False
    if place.anywhere or not job_location.strip():
        return True
    return any(location_matches(job_location, t) for t in place.targets)


# ── salary ───────────────────────────────────────────────────────────────
def salary_ok(status: dict, salary_max: int | None, currency: str | None) -> bool:
    floor = status.get("salary_min")
    if not floor or salary_max is None or currency not in (None, "USD"):
        return True  # unknown pay never excludes
    return salary_max >= floor


# ── visa sponsorship ─────────────────────────────────────────────────────
_NO_SPONSOR = re.compile(
    r"(?:unable|not able|cannot|can't|can not|do not|don't|does not|will not|won't|not)\s+(?:to\s+|be able to\s+)?"
    r"(?:provide|offer|sponsor|support)[^.\n]{0,50}(?:visa|sponsorship|work authori[sz]ation)"
    r"|no\s+(?:visa\s+)?sponsorship"
    r"|without\s+(?:requiring\s+|the need for\s+)?(?:visa\s+)?sponsorship"
    r"|(?:u\.?s\.?|united states)\s+citizen(?:ship)?\s+(?:is\s+)?required"
    r"|must\s+be\s+(?:a\s+)?(?:u\.?s\.?|united states)\s+citizen"
    r"|security clearance|ts/sci",
    re.IGNORECASE,
)


def refuses_sponsorship(description: str) -> bool:
    return bool(_NO_SPONSOR.search(description or ""))


# ── required experience ─────────────────────────────────────────────────
_YEARS = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|to)?\s*(?:\d{1,2})?\s*\+?\s*years?", re.IGNORECASE)
ENTRY_LEVELS = {"intern", "new_grad", "junior"}
ENTRY_MAX_YEARS = 3  # an entry-level candidate isn't shown jobs that ask for this many years or more


def required_years(description: str) -> int | None:
    """The main 'N+ years of experience' requirement in a posting (largest lower bound), if any."""
    best = None
    for m in _YEARS.finditer(description or ""):
        context = description[m.end() : m.end() + 70].lower()
        if "experience" not in context and "experienced" not in context:
            continue
        n = int(m.group(1))
        if 1 <= n <= 20:
            best = n if best is None else max(best, n)
    return best


def too_experienced(description: str, acceptable: set[str] | None) -> bool:
    """True when the candidate only wants entry-level roles but the posting asks for years of experience."""
    if acceptable is None or not acceptable <= ENTRY_LEVELS:
        return False
    years = required_years(description)
    return years is not None and years >= ENTRY_MAX_YEARS


# ── scoring ──────────────────────────────────────────────────────────────
def profile_skills(background: dict) -> list[str]:
    seen: dict[str, None] = {}
    for s in background.get("skills") or []:
        if s.get("name"):
            seen[s["name"].strip().lower()] = None
    for item in (background.get("experience") or []) + (background.get("projects") or []):
        for t in item.get("technologies") or []:
            seen[t.strip().lower()] = None
    return [s for s in seen if len(s) >= 2]


def _skill_pattern(skill: str) -> re.Pattern[str]:
    # word-ish boundaries that also work for names like "c++", "c#", "node.js"
    return re.compile(rf"(?<![a-z0-9]){re.escape(skill)}(?![a-z0-9])")


_TOKEN = re.compile(r"[a-z0-9+#.]+")


def _tokens(s: str) -> set[str]:
    return {t.strip(".") for t in _TOKEN.findall(s.lower()) if t.strip(".")}


def title_similarity(title: str, roles: list[str]) -> float:
    if not roles:
        return 0.5  # nothing to compare against: neutral
    tt = _tokens(title)
    best = 0.0
    for role in roles:
        rt = _tokens(role)
        if rt:
            best = max(best, len(rt & tt) / len(rt))
    return best


def skill_fraction(skills: list[str], title: str, description: str) -> float:
    if not skills:
        return 0.5
    text = f"{title.lower()} {title.lower()} {(description or '').lower()}"
    hits = sum(1 for s in skills if _skill_pattern(s).search(text))
    return min(1.0, hits / max(4, min(len(skills), 8)))


def recency(posted_at: datetime | None, now: datetime | None = None) -> float:
    if posted_at is None:
        return 0.3
    age_days = ((now or datetime.now(UTC)) - posted_at).total_seconds() / 86400
    return max(0.0, min(1.0, 1 - (age_days - 7) / 53)) if age_days > 7 else 1.0


def prestige_fit(company_tier: int | None, preference: int | None) -> float:
    if company_tier is None or not preference or preference <= 2:
        return 0.5
    return min(1.0, company_tier / preference)


def prefilter_score(
    *, title: str, description: str, posted_at: datetime | None, company_tier: int | None,
    company_size: str | None, company_industry: str | None, status: dict, skills: list[str],
    now: datetime | None = None,
) -> float:
    base = (
        45 * title_similarity(title, status.get("target_roles") or [])
        + 35 * skill_fraction(skills, title, description)
        + 10 * recency(posted_at, now)
        + 10 * prestige_fit(company_tier, status.get("prestige_preference"))
    )
    sizes = status.get("company_sizes") or []
    if sizes and company_size and company_size not in sizes:
        base -= 5
    industries = [i.lower() for i in status.get("industries") or []]
    if company_industry and any(i in company_industry.lower() or company_industry.lower() in i for i in industries):
        base += 5
    return max(0.0, min(100.0, base))
