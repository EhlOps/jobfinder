"""Cheap, deterministic filtering and ranking that runs before any LLM call.

Hard filters drop jobs the candidate clearly can't or won't take (wrong level, below their salary
floor, needs sponsorship the job refuses, wrong place). A 0-100 prefilter score then ranks what's
left so the limited LLM budget goes to the most promising jobs first. Unknown data never excludes
a job: if a posting doesn't say where/what level/how much, it stays and the LLM judges it."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

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


# ── career stage (from the graduation date) ──────────────────────────────
STUDENT, FINAL_YEAR, RECENT_GRAD, NEW_GRAD, GRADUATED = "student", "final_year", "recent_grad", "new_grad", "graduated"
RECENT_GRAD_MONTHS = 24
# What each stage may apply to when the candidate didn't pick levels themselves.
STAGE_LEVELS = {
    STUDENT: {"intern"},
    FINAL_YEAR: {"intern", "new_grad", "junior"},
    RECENT_GRAD: {"new_grad", "junior"},
    NEW_GRAD: {"new_grad", "junior"},
}
_YEAR_MONTH = re.compile(r"^\s*(\d{4})-(\d{1,2})")


def _month_index(year: int, month: int) -> int:
    return year * 12 + month - 1


def graduation_month(status: dict) -> int | None:
    """The graduation date ('YYYY-MM') as a month index, or None when missing or unreadable."""
    m = _YEAR_MONTH.match(str(status.get("graduation_date") or ""))
    if not m or not 1 <= int(m.group(2)) <= 12:
        return None
    return _month_index(int(m.group(1)), int(m.group(2)))


def months_to_graduation(status: dict, today: date | None = None) -> int | None:
    """Months until graduation (negative once it has passed)."""
    g = graduation_month(status)
    t = today or datetime.now(UTC).date()
    return None if g is None else g - _month_index(t.year, t.month)


def career_stage(status: dict, today: date | None = None) -> str | None:
    """student (>12 months to go), final_year, recent_grad (within 24 months after), graduated, or None
    when there is nothing to go on. Without a date, 'new grad' still counts as is_new_grad says."""
    months = months_to_graduation(status, today)
    if months is None:
        return NEW_GRAD if status.get("is_new_grad") else None
    if months > 12:
        return STUDENT
    if months >= 0:
        return FINAL_YEAR
    return RECENT_GRAD if months >= -RECENT_GRAD_MONTHS else GRADUATED


def acceptable_seniorities(status: dict, today: date | None = None) -> set[str] | None:
    """None means 'don't filter on level'."""
    chosen = status.get("seniority") or []
    if not chosen:
        if status.get("is_new_grad") is False:  # an experienced candidate (e.g. a part-time degree): no stage filter
            return None
        return STAGE_LEVELS.get(career_stage(status, today) or "")
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
    r"|(?:unable|not able|cannot|can't|can not|do not|don't|does not|will not|won't)\s+(?:to\s+)?sponsor\b(?!\s+(?:events?|conferences?|relocation|meetups?))"
    r"|sponsorship\s+(?:(?:is|are|will)\s+not\s+(?:be\s+|currently\s+)?|(?:isn't|aren't)\s+(?:currently\s+)?|(?:is\s+)?un)(?:available|provided|offered|supported)"
    r"|no\s+(?:(?:work\s+)?visa\s+|h-?1-?b\s+|immigration\s+)?sponsorship"
    r"|without\s+(?:requiring\s+|the need for\s+)?(?:visa\s+)?sponsorship"
    r"|(?:u\.?s\.?|united states)\s+citizen(?:ship)?\s+(?:is\s+)?required"
    r"|must\s+be\s+(?:a\s+)?(?:u\.?s\.?|united states)\s+citizen"
    r"|security clearance|ts/sci",
    re.IGNORECASE,
)


def refuses_sponsorship(description: str) -> bool:
    return bool(_NO_SPONSOR.search(description or ""))


# OPT/CPT are deliberately not in _VISA: they need no employer sponsorship, so "OPT welcome" is no signal.
# A positive signal needs a visa/immigration term: "sponsor" alone also means events, certifications, 401(k)s.
_VISA = (
    r"\b(?:visas?|h-?1-?b|immigration|work\s+(?:permits?|authori[sz]ation)|employment\s+authori[sz]ation"
    r"|green\s*cards?)\b"
)
_SPONSORS = re.compile(
    r"(?:will|can|do|does|happy to|able to|willing to)\s+(?:also\s+)?(?:provide\s+|offer\s+)?"
    rf"(?:{_VISA}[^.\n]{{0,20}}sponsor|sponsor\b[^.\n]{{0,40}}{_VISA})"
    rf"|(?:we|company)\s+sponsors?\b[^.\n]{{0,40}}{_VISA}"
    rf"|(?:offer|provide|provides|offers|available)[^.\n]{{0,30}}{_VISA}\s+sponsorship"
    rf"|(?:offer|provide|provides|offers)[^.\n]{{0,30}}sponsorship[^.\n]{{0,30}}{_VISA}"
    rf"|{_VISA}\s+sponsorship\s+(?:is\s+|may be\s+)?(?:available|offered|provided)"
    r"|h-?1-?b[^.\n]{0,40}(?:sponsor|transfers?\s+(?:are\s+)?(?:welcome|accepted|ok))"
    r"|sponsor[^.\n]{0,40}h-?1-?b",
    re.IGNORECASE,
)


def sponsorship_signal(description: str) -> str | None:
    """'refuses', 'sponsors' or None (silent). A refusal wins over a friendly-sounding phrase."""
    if refuses_sponsorship(description):
        return "refuses"
    return "sponsors" if _SPONSORS.search(description or "") else None


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


# ── graduation windows in postings ───────────────────────────────────────
_MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_GRAD_TRIGGER = re.compile(r"\b(?:graduating|graduation|class of)\b", re.IGNORECASE)
_DATE_TOKEN = re.compile(r"(?:\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+)?\b(20\d\d)\b", re.IGNORECASE)
_BEFORE_WORDS = re.compile(r"\b(?:by|before|prior to|no later than|until)\s*$", re.IGNORECASE)
_AFTER_WORDS = re.compile(r"\b(?:after|since|no earlier than|not earlier than|not before)\s*$", re.IGNORECASE)
_OR_LATER = re.compile(r"^\s*(?:,?\s*(?:or|and)\s+(?:later|after|afterwards|newer|onwards?)|\+|onwards?)(?=\s*(?:$|[.,;:)]))", re.IGNORECASE)
_OR_EARLIER = re.compile(r"^\s*,?\s*(?:or|and)\s+(?:earlier|before|prior)(?=\s*(?:$|[.,;:)]))", re.IGNORECASE)
GRAD_WINDOW_SLACK = 1  # months


def graduation_window(description: str) -> tuple[int | None, int | None] | None:
    """The graduation dates a posting accepts, as (first, last) month indexes (None = open ended), from
    phrases like 'graduating between December 2026 and June 2027', 'class of 2027' or 'graduating by May 2027'.
    A lone date covers its whole year, since a single month rarely means an exact cut-off; 'after', 'or later'
    and 'no earlier than' make it an open-ended lower bound, 'by', 'before' and 'or earlier' an upper one."""
    text = description or ""
    for trig in _GRAD_TRIGGER.finditer(text):
        span = text[trig.end() : trig.end() + 90]
        tokens = list(_DATE_TOKEN.finditer(span))
        if not tokens:
            continue
        first, last = tokens[0], tokens[-1]
        year = int(first.group(2))
        named = _MONTHS[first.group(1).lower()[:3]] if first.group(1) else None
        before = span[: first.start()]
        after = span[first.end() : tokens[1].start() if len(tokens) >= 2 else None]  # up to the next date
        if _AFTER_WORDS.search(before) or _OR_LATER.search(after):
            return _month_index(year, named or 1), None
        if _BEFORE_WORDS.search(before) or _OR_EARLIER.search(after):
            return None, _month_index(year, named or 12)
        if len(tokens) >= 2:
            lo_month = _MONTHS[first.group(1).lower()[:3]] if first.group(1) else 1
            hi_month = _MONTHS[last.group(1).lower()[:3]] if last.group(1) else 12
            lo, hi = _month_index(year, lo_month), _month_index(int(last.group(2)), hi_month)
            return (lo, hi) if lo <= hi else None
        return _month_index(year, 1), _month_index(year, 12)
    return None


def outside_grad_window(description: str, status: dict) -> bool:
    """True only when the posting names graduation dates, the candidate's date is known, and it falls outside."""
    grad = graduation_month(status)
    if grad is None:
        return False
    window = graduation_window(description)
    if window is None:
        return False
    lo, hi = window
    return (lo is not None and grad < lo - GRAD_WINDOW_SLACK) or (hi is not None and grad > hi + GRAD_WINDOW_SLACK)


EARLY_CAREER_TITLE = re.compile(
    r"\b(new grad(?:uate)?|recent graduate|graduate (?:engineer|developer|program(?:me)?|scheme)s?"
    r"|university (?:grad(?:uate)?|hire|program(?:me)?|recruit(?:ing)?)s?|campus (?:hire|recruit(?:ing)?)s?|early career|entry[- ]level)\b",
    re.IGNORECASE,
)
# Same words for Postgres (\y is its word boundary); keep in step with EARLY_CAREER_TITLE.
EARLY_CAREER_SQL = (
    r"\y(new grad(uate)?|recent graduate|graduate (engineer|developer|program(me)?|scheme)s?"
    r"|university (grad(uate)?|hire|program(me)?|recruit(ing)?)s?|campus (hire|recruit(ing)?)s?|early career|entry[- ]level)\y"
)
_INTERN_TITLE = re.compile(r"\b(intern|internship|co-?op)\b", re.IGNORECASE)
STAGE_BOOST = 10


def is_early_career(title: str, seniority: str | None) -> bool:
    return seniority in ("intern", "new_grad") or bool(EARLY_CAREER_TITLE.search(title) or _INTERN_TITLE.search(title))


def early_career_boost(title: str, seniority: str | None, stage: str | None) -> float:
    """Roles aimed at the candidate's stage: new-grad and early-career roles for people about to graduate or
    just out; internships for students and those in their last year."""
    intern = seniority == "intern" or bool(_INTERN_TITLE.search(title))
    if stage in (FINAL_YEAR, RECENT_GRAD, NEW_GRAD) and is_early_career(title, seniority):
        return STAGE_BOOST
    if stage in (STUDENT, FINAL_YEAR) and intern:
        return STAGE_BOOST
    return 0.0


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


def sponsorship_adjustment(status: dict, signal: str | None, company_sponsors: bool | None) -> float:
    """Only for candidates who need sponsorship; unknown (no signal, no company flag) is neutral."""
    if status.get("needs_visa_sponsorship") is not True:
        return 0.0
    if signal == "refuses":
        return -10.0
    if signal == "sponsors":
        return 8.0
    return {True: 4.0, False: -4.0}.get(company_sponsors, 0.0)  # company flag only when the posting is silent


def prefilter_score(
    *, title: str, description: str, posted_at: datetime | None, company_tier: int | None,
    company_size: str | None, company_industry: str | None, status: dict, skills: list[str],
    now: datetime | None = None, seniority: str | None = None, stage: str | None = None,
    sponsorship: str | None = None, company_sponsors: bool | None = None,
) -> float:
    base = (
        45 * title_similarity(title, status.get("target_roles") or [])
        + 35 * skill_fraction(skills, title, description)
        + 10 * recency(posted_at, now)
        + 10 * prestige_fit(company_tier, status.get("prestige_preference"))
        + early_career_boost(title, seniority, stage)
        + sponsorship_adjustment(status, sponsorship, company_sponsors)
    )
    sizes = status.get("company_sizes") or []
    if sizes and company_size and company_size not in sizes:
        base -= 5
    industries = [i.lower() for i in status.get("industries") or []]
    if company_industry and any(i in company_industry.lower() or company_industry.lower() in i for i in industries):
        base += 5
    return max(0.0, min(100.0, base))
