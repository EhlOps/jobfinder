"""Rung 3: the page carries a schema.org JobPosting in JSON-LD, so at least this one job can be kept.

Never finds a board (SingleJob only); the ladder keeps the posting and carries on looking. JSON-LD comes from a
third-party page, so parsing is bounded (block count, block size, nesting, nodes visited) and never evaluates
anything; every text field is reduced to plain text and the URL is the page's own, http(s) only.
"""
import hashlib
import html as htmllib
import json
import re
from datetime import UTC, datetime, timedelta

from jobfinder.core.urls import registrable_domain, safe_http_url
from jobfinder.models.posting import (
    JobPosting,
    clip,
    html_to_text,
    infer_seniority,
    infer_workplace,
    parse_salary_range,
)
from jobfinder.services.resolver.ladder import ResolveContext, SingleJob
from jobfinder.services.resolver.names import name_from_domain

MAX_BLOCKS = 20
MAX_BLOCK_CHARS = 1_000_000
MAX_NODES = 500  # values looked at per block
MAX_DEPTH = 6  # list -> @graph -> WebPage -> mainEntity -> JobPosting is about as deep as real pages go

_LD_OPEN_RE = re.compile(r"<script\b[^>]{0,500}?\btype\s*=\s*[\"']?application/ld\+json[\"']?[^>]{0,500}>",
                         re.IGNORECASE)
_CLOSE_RE = re.compile(r"</script\s*>", re.IGNORECASE)
_WRAPPER_RE = re.compile(r"^\s*(?:<!--|(?://|/\*)?\s*<!\[CDATA\[(?:\s*\*/)?)"
                         r"|(?:-->|(?://|/\*)?\s*\]\]>(?:\s*\*/)?)\s*$")
_DECODER = json.JSONDecoder(strict=False)  # real pages put raw newlines inside strings


def _decode(raw: str):
    """One JSON-LD block, tolerating comment/CDATA wrappers, trailing junk and an entity-escaped body."""
    text = raw
    for _ in range(3):  # "<!-- //<![CDATA[" stacks wrappers
        text = _WRAPPER_RE.sub("", text).strip()
    for candidate in (text, htmllib.unescape(text)) if "&quot;" in text else (text,):
        for start in sorted(i for i in {candidate.find("{"), candidate.find("[")} if i >= 0):
            try:
                return _DECODER.raw_decode(candidate, start)[0]
            except (ValueError, RecursionError):  # malformed, or a nesting bomb
                continue
    return None


def ld_blocks(page_html: str):
    """The raw text of each JSON-LD script, at most MAX_BLOCKS. Linear in the page size: an unclosed block
    ends the scan rather than being searched again from every later opening tag."""
    pos = 0
    for _ in range(MAX_BLOCKS):
        m = _LD_OPEN_RE.search(page_html, pos)
        end = _CLOSE_RE.search(page_html, m.end()) if m else None
        if end is None:
            return
        yield page_html[m.end():end.start()]
        pos = end.end()


def _is_posting(node: dict) -> bool:
    t = node.get("@type")
    return any(isinstance(x, str) and x.rsplit("/", 1)[-1] == "JobPosting" for x in (t if isinstance(t, list) else [t]))


def find_job_posting(page_html: str) -> dict | None:
    """The first JobPosting object in the page's JSON-LD (top level, arrays, @graph or nested under another
    node such as WebPage.mainEntity), or None. Depth-first, at most MAX_NODES values per block."""
    for raw in ld_blocks(page_html):
        if len(raw) > MAX_BLOCK_CHARS:
            continue
        stack, budget = [(_decode(raw), 0)], MAX_NODES
        while stack and budget > 0:
            node, depth = stack.pop()
            budget -= 1
            if isinstance(node, dict):
                if _is_posting(node):
                    return node
                children = list(node.values())
            elif isinstance(node, list):
                children = node[:MAX_NODES]
            else:
                continue
            if depth < MAX_DEPTH:
                stack.extend((c, depth + 1) for c in reversed(children) if isinstance(c, dict | list))
    return None


def _text(v, limit: int = 300) -> str:
    """Plain one-line text from a JSON-LD value (tags dropped, entities decoded)."""
    if isinstance(v, list):
        v = v[0] if v else ""
    if isinstance(v, dict):
        v = v.get("name") or v.get("@value")
    if not isinstance(v, str | int | float) or isinstance(v, bool):
        return ""
    return re.sub(r"\s+", " ", html_to_text(htmllib.unescape(str(v)))).strip()[:limit]


def _date(v) -> datetime | None:
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip()
    for candidate in (s, s[:10]):
        try:
            d = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)
    return None


def _place(p) -> str:
    if isinstance(p, str):
        return _text(p)
    if not isinstance(p, dict):
        return ""
    addr = p.get("address")
    if isinstance(addr, str) or not isinstance(addr, dict):
        return _text(addr) or _text(p.get("name"))
    bits = [_text(addr.get(k), 100) for k in ("addressLocality", "addressRegion", "addressCountry")]
    return ", ".join(dict.fromkeys(b for b in bits if b)) or _text(addr.get("streetAddress"))


def _location(ld: dict) -> str:
    raw = ld.get("jobLocation")
    places = [_place(p) for p in (raw if isinstance(raw, list) else [raw])[:20]]
    if not any(places):  # remote jobs often name only where applicants may live
        req = ld.get("applicantLocationRequirements")
        places = [_text(r) for r in (req if isinstance(req, list) else [req])[:20]]
    return "; ".join(dict.fromkeys(p for p in places if p))[:500]


def _remote(ld: dict) -> bool:
    t = ld.get("jobLocationType")
    return "TELECOMMUTE" in [str(x).upper() for x in (t if isinstance(t, list) else [t])]


def _number(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        n = v
    elif isinstance(v, str):
        try:
            n = float(v.replace(",", "").strip())
        except ValueError:
            return None
    else:
        return None
    return int(n) if 0 < n < 100_000_000 else None


def _salary(ld: dict) -> tuple[int | None, int | None, str | None]:
    """baseSalary when it's annual (MonetaryAmount -> QuantitativeValue with unitText YEAR)."""
    sal = ld.get("baseSalary")
    if isinstance(sal, list):
        sal = sal[0] if sal else None
    if not isinstance(sal, dict):
        return None, None, None
    val = sal.get("value")
    unit = str((val.get("unitText") if isinstance(val, dict) else None) or sal.get("unitText") or "").upper()
    if unit != "YEAR":
        return None, None, None
    if isinstance(val, dict):
        lo = _number(val.get("minValue")) or _number(val.get("value"))
        hi = _number(val.get("maxValue"))
    else:
        lo, hi = _number(val), None
    if lo is None and hi is None:
        return None, None, None
    cur = str(sal.get("currency") or "").strip().upper()
    return lo, hi or lo, cur if re.fullmatch(r"[A-Z]{3}", cur) else None


def _employment(v) -> str | None:
    vals = [_text(x, 50) for x in (v if isinstance(v, list) else [v])[:5]]
    return ", ".join(x for x in vals if x)[:100] or None


def _org_domain(org) -> str:
    """The hiring organisation's own website, if the JSON-LD names one."""
    if not isinstance(org, dict):
        return ""
    for key in ("sameAs", "url"):
        v = org.get(key)
        for u in (v if isinstance(v, list) else [v])[:5]:
            url = safe_http_url(u if isinstance(u, str) else None)
            if url:
                return registrable_domain(url)
    return ""


def posting_from_ld(ld: dict, page_url: str) -> JobPosting | None:
    """A JobPosting from a schema.org JobPosting object, or None without a title."""
    title = _text(ld.get("title")) or _text(ld.get("name"))
    if not title:
        return None
    desc = ld.get("description")
    desc = desc if isinstance(desc, str) else ""
    if "&lt;" in desc:  # some sites entity-escape the HTML inside the JSON
        desc = htmllib.unescape(desc)
    description = clip(html_to_text(desc))
    location = _location(ld)
    url = safe_http_url(page_url)
    company = _text(ld.get("hiringOrganization"), 200) or name_from_domain(registrable_domain(url))
    lo, hi, cur = _salary(ld)
    if lo is None and hi is None:
        lo, hi, cur = parse_salary_range(description) or (None, None, None)
    return JobPosting(
        source="jsonld",
        external_id=hashlib.sha1(url.encode()).hexdigest(),
        company_name=company,
        title=title,
        url=url,
        location=location,
        workplace_type=infer_workplace(title, location, declared="remote" if _remote(ld) else None),
        employment_type=_employment(ld.get("employmentType")),
        description_text=description,
        salary_min=lo,
        salary_max=hi,
        salary_currency=cur,
        seniority=infer_seniority(title),
        posted_at=_date(ld.get("datePosted")),
    )


class JsonLdRung:
    name = "jsonld"
    order = 3.0

    async def run(self, ctx: ResolveContext) -> SingleJob | None:
        page = await ctx.page()
        if page is None:
            return None
        ld = find_job_posting(page.text)
        if ld is None:
            return None
        until = _date(ld.get("validThrough"))
        if until is not None and len(str(ld["validThrough"]).strip()) <= 10:
            until += timedelta(days=1)  # a bare date is valid through the end of that day
        if until is not None and until < datetime.now(UTC):
            ctx.note(f"jsonld: posting expired {until.date().isoformat()}")
            return None
        posting = posting_from_ld(ld, page.url)
        if posting is None:
            ctx.note("jsonld: JobPosting without a title")
            return None
        org = _org_domain(ld.get("hiringOrganization"))
        here = registrable_domain(page.url)
        if org and org != here:  # e.g. an aggregator re-posting the job; the board is on the company's site
            ctx.note(f"jsonld: hiring organisation is at {org}, not {here}")
        return SingleJob(posting)
