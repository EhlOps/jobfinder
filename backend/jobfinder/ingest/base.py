"""Common job-posting shape and the text/field normalisation every source shares."""
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

MAX_DESCRIPTION_CHARS = 20_000


@dataclass
class JobPosting:
    source: str
    external_id: str
    company_name: str
    title: str
    url: str
    location: str = ""
    workplace_type: str | None = None  # remote|hybrid|onsite
    employment_type: str | None = None
    description_text: str = ""
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    seniority: str | None = None
    posted_at: datetime | None = None

    @property
    def dedupe_hash(self) -> str:
        return dedupe_hash(self.company_name, self.title, self.location)


def safe_http_url(url: str | None) -> str:
    """The URL if it is an absolute http(s) link, else "". Job data comes from third parties, so a
    javascript: or data: link must never reach an href."""
    url = (url or "").strip()
    p = urlparse(url)
    return url if p.scheme in ("http", "https") and p.netloc else ""


# ── text ──────────────────────────────────────────────────────────────────
_BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "table"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")
            if tag == "li":
                self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    p.close()
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in "".join(p.parts).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:MAX_DESCRIPTION_CHARS]


def clip(text: str) -> str:
    return text.strip()[:MAX_DESCRIPTION_CHARS]


# ── derived fields ────────────────────────────────────────────────────────
def dedupe_hash(company: str, title: str, location: str) -> str:
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    return hashlib.sha1(f"{norm(company)}|{norm(title)}|{norm(location)}".encode()).hexdigest()


def infer_workplace(*texts: str | None, declared: str | None = None) -> str | None:
    """`declared` is the ATS's own field when it has one; otherwise look for words in the text."""
    if declared:
        d = declared.lower().replace("-", "").replace("_", "").replace(" ", "")
        if d in ("remote", "hybrid", "onsite"):
            return d
    blob = " ".join(t for t in texts if t).lower()
    if "hybrid" in blob:
        return "hybrid"
    if "remote" in blob:
        return "remote"
    return None


_SENIORITY_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("intern", re.compile(r"\b(intern|internship|co-?op)\b", re.IGNORECASE)),
    ("new_grad", re.compile(r"\b(new grad|new graduate|university grad|graduate|early career|entry[- ]level)\b", re.IGNORECASE)),
    ("manager", re.compile(r"\b(manager|director|vp|vice president|head of|chief)\b", re.IGNORECASE)),
    ("staff", re.compile(r"\b(staff|principal|distinguished|fellow)\b", re.IGNORECASE)),
    # roman-numeral ladders: Engineer I is junior, II mid-level, III senior, IV staff
    ("staff", re.compile(r"\b(?:engineer|developer|scientist|analyst|swe|sde)\s+iv\b", re.IGNORECASE)),
    ("senior", re.compile(r"\b(senior|sr\.?|lead|(?:engineer|developer|scientist|analyst|swe|sde)\s+iii)\b", re.IGNORECASE)),
    ("mid", re.compile(r"\b(?:engineer|developer|scientist|analyst|swe|sde)\s+ii\b", re.IGNORECASE)),
    ("junior", re.compile(r"\b(junior|jr\.?|associate|engineer i|swe i)\b", re.IGNORECASE)),
]


def infer_seniority(title: str) -> str | None:
    for level, pat in _SENIORITY_RULES:
        if pat.search(title):
            return level
    return None


_MONEY = r"\$\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?([kK])?"
_RANGE = re.compile(_MONEY + r"\s*(?:-|–|—|to)\s*" + _MONEY)


def _amount(num: str, k: str | None) -> float:
    return float(num.replace(",", "")) * (1000 if k else 1)


def parse_salary_range(text: str) -> tuple[int, int, str] | None:
    """Best-effort annual USD range from free text like '$120,000 - $150,000' or '$120K–$150K'.
    Ignores anything outside a plausible annual-salary band (so hourly rates are skipped)."""
    for m in _RANGE.finditer(text):
        lo, hi = _amount(m.group(1), m.group(2)), _amount(m.group(3), m.group(4))
        if lo > hi:
            continue
        if m.group(4) and not m.group(2) and lo < 1000:  # "$120 - $150K": the K applies to both
            lo *= 1000
        if 20_000 <= lo and hi <= 1_500_000:
            return int(lo), int(hi), "USD"
    return None
