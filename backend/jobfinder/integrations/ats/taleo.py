"""Oracle Taleo Enterprise career sections ({tenant}.taleo.net/careersection/{csName}/...).

Slug format: "{tenant}__{csName}", e.g. "cu__2" for cu.taleo.net/careersection/2/jobsearch.ftl.
The listing is a JSON API keyed by the tenant's numeric portal id, which only appears in the career section's
own search page, so that page is fetched first. Many tenants have moved to Oracle Recruiting Cloud; their
sections still answer HTTP 200 with a "Career Section Unavailable" page, which is reported as an error.
"""
import asyncio
import json
import re
from datetime import UTC, datetime
from urllib.parse import unquote

import httpx

from jobfinder.ingest.base import (
    JobPosting,
    clip,
    html_to_text,
    infer_seniority,
    infer_workplace,
    parse_salary_range,
    safe_http_url,
)
from jobfinder.integrations.robots import PoliteFetcher

BASE = "https://{tenant}.taleo.net/careersection"
SEARCH_PAGE = BASE + "/{cs}/jobsearch.ftl?lang=en"
LIST_API = BASE + "/rest/jobboard/searchjobs?lang=en&portal={portal}"
JOB_URL = BASE + "/{cs}/jobdetail.ftl?job={job}&lang=en"
PAGE = 25  # fixed by Taleo
MAX_PAGES = 80  # 2000 postings per board
MAX_DETAILS = 150  # per-board cap on job-page fetches; the rest are listed without a description
DETAIL_CONCURRENCY = 4
MAX_REDIRECTS = 3
# Taleo answers the search API with an empty body unless the client says which timezone it is in
API_HEADERS = {"Content-Type": "application/json", "Accept": "application/json", "tz": "GMT+00:00"}
# Taleo's 302s claim gzip but send a plain body, which httpx refuses to decode; pages are asked for uncompressed
PAGE_HEADERS = {"Accept-Encoding": "identity"}

_TENANT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_CS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_JOB_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
# the section's own config ("portalNo: '101430233'", "portal":101430233) before portal= in a link
_PORTAL_CONFIG_RE = re.compile(r"""portalNo["']?\s*:\s*["']?(\d{3,15})\b|["']portal["']\s*:\s*["']?(\d{3,15})\b""")
_PORTAL_LINK_RE = re.compile(r"""careersection/([A-Za-z0-9_-]{1,64})/[^"'\s<>]*?[?&;]portal=(\d{3,15})\b""")
_PORTAL_ANY_RE = re.compile(r"[?&;]portal=(\d{3,15})\b")
# path segments under /careersection/ that are shared Taleo apps, not career sections
_NOT_SECTIONS = {"rest", "theme", "iam", "sharedinterface", "careersection"}
_UNAVAILABLE_RE = re.compile(r"<title>\s*Career Section Unavailable", re.IGNORECASE)
_DESC_LIST_RE = re.compile(r"fillList\(\s*'requisitionDescriptionInterface'\s*,\s*'descRequisition'\s*,\s*\[")
# one element of a JS array literal: a single-quoted string or a bare token, then "," or "]"
_JS_ITEM_RE = re.compile(r"\s*(?:'((?:[^'\\]|\\.)*)'|[A-Za-z0-9.+-]*)\s*([,\]])", re.DOTALL)
_JS_ESC_RE = re.compile(r"\\(u[0-9a-fA-F]{4}|.)", re.DOTALL)
_DATE_FORMATS = (
    "%b %d, %Y", "%B %d, %Y", "%b %d, %Y, %I:%M:%S %p", "%d-%b-%Y", "%Y-%m-%d",
    "%d.%m.%Y", "%d %b %Y", "%d %B %Y",
)  # no "nn/nn/yyyy": month-first and day-first tenants can't be told apart


class CareerSectionUnavailable(RuntimeError):
    """The tenant or career section is switched off (often after a move to Oracle Recruiting Cloud)."""


def parse_slug(slug: str) -> tuple[str, str]:
    """(tenant, csName) from "{tenant}__{csName}"; ValueError when malformed."""
    parts = (slug or "").split("__")
    if len(parts) != 2 or not (_TENANT_RE.match(parts[0]) and _CS_RE.match(parts[1])):
        raise ValueError(f"invalid taleo slug '{slug}' (expected tenant__careersection)")
    return parts[0], parts[1]


def make_slug(tenant: str, cs: str) -> str | None:
    slug = f"{(tenant or '').lower()}__{cs or ''}"
    try:
        parse_slug(slug)
    except ValueError:
        return None
    return slug


def taleo_board(sub: str, path: str) -> tuple[str, str] | None:
    """{tenant}.taleo.net/careersection/{csName}/... -> ("taleo", "tenant__csName")."""
    segs = [s for s in path.split("/") if s]
    if "." in sub or len(segs) < 2 or segs[0].lower() != "careersection" or segs[1].lower() in _NOT_SECTIONS:
        return None
    slug = make_slug(sub, segs[1])
    return ("taleo", slug) if slug else None


def extract_portal(page_html: str, cs: str | None = None) -> str | None:
    """The numeric portal id: the page's own config, else a link to this section, else any portal= link."""
    page = page_html or ""
    m = _PORTAL_CONFIG_RE.search(page)
    if m:
        return m.group(1) or m.group(2)
    for section, portal in _PORTAL_LINK_RE.findall(page):
        if cs and section.lower() == cs.lower():
            return portal
    m = _PORTAL_ANY_RE.search(page)
    return m.group(1) if m else None


def parse_date(text) -> datetime | None:
    if not isinstance(text, str) or not text.strip() or len(text) > 40:
        return None
    t = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _js_unescape(s: str) -> str:
    def one(m: re.Match) -> str:
        c = m.group(1)
        if len(c) == 5:
            return chr(int(c[1:], 16))
        return {"n": "\n", "t": "\t", "r": "\r"}.get(c, c)

    return _JS_ESC_RE.sub(one, s)


def _js_strings(text: str, start: int) -> list[str]:
    """The string elements of the JS array literal whose "[" ends at `start` (non-strings become "")."""
    out, pos = [], start
    while pos < len(text):
        m = _JS_ITEM_RE.match(text, pos)
        if not m:
            break
        out.append(_js_unescape(m.group(1)) if m.group(1) is not None else "")
        pos = m.end()
        if m.group(2) == "]":
            break
    return out


def job_description(page_html: str) -> str:
    """Plain-text description from a jobdetail.ftl page ("" when it cannot be found).
    The page hands its fields to `api.fillList(..., 'descRequisition', [...])`; rich-text fields
    (description, qualifications, ...) are URL-encoded HTML marked with a "!*!" prefix, each repeated twice."""
    m = _DESC_LIST_RE.search(page_html or "")
    if not m:
        return ""
    parts: list[str] = []
    for item in _js_strings(page_html, m.end()):
        if item.startswith("!*!"):
            chunk = unquote(item[3:])
            if chunk.strip() and chunk not in parts:
                parts.append(chunk)
    return html_to_text("\n".join(parts)) if parts else ""


def _location(cell: str) -> str:
    try:
        locs = json.loads(cell)
    except ValueError:
        return cell.strip()
    if isinstance(locs, str):
        return locs.strip()
    if isinstance(locs, list):
        return "; ".join(x.strip() for x in locs if isinstance(x, str) and x.strip())[:500]
    return ""  # null / numbers / objects are not a place


def _columns(r: dict) -> tuple[str, str, datetime | None]:
    """(title, location, posted_at) from a requisition's tenant-configured columns."""
    cols = [c if isinstance(c, str) else "" for c in (r.get("column") or [])]
    if not cols:
        return "", "", None
    linked = r.get("linkedColumn")
    t_idx = linked if isinstance(linked, int) and 0 <= linked < len(cols) else 0
    loc_idx = [i for i in (r.get("locationsColumns") or []) if isinstance(i, int) and 0 <= i < len(cols)]
    if not loc_idx:  # no hint: the first column that looks like a JSON array
        loc_idx = [i for i, c in enumerate(cols) if i != t_idx and c.strip().startswith("[")][:1]
    location = "; ".join(x for x in (_location(cols[i]) for i in loc_idx) if x)[:500]
    posted = next((d for i, c in enumerate(cols) if i != t_idx and i not in loc_idx and (d := parse_date(c))), None)
    return cols[t_idx].strip(), location, posted


def _job_no(r: dict) -> str:
    """The number the public job URL uses (contestNo), else the internal jobId."""
    for key in ("contestNo", "jobId"):
        v = str(r.get(key) or "").strip()
        if _JOB_RE.match(v):
            return v
    return ""


def parse_taleo(requisitions: list[dict], company_name: str, slug: str) -> list[JobPosting]:
    """`requisitions` are searchjobs `requisitionList` entries; a "description" key may hold the job page text."""
    tenant, cs = parse_slug(slug)
    out = []
    for r in requisitions:
        if not isinstance(r, dict):
            continue
        title, location, posted = _columns(r)
        job = _job_no(r)
        if not title or not job:
            continue
        desc = str(r.get("description") or "")
        sal = parse_salary_range(desc)
        out.append(
            JobPosting(
                source="taleo",
                external_id=f"{tenant}:{job}",  # requisition numbers are only unique within a tenant
                company_name=company_name,
                title=title,
                url=safe_http_url(JOB_URL.format(tenant=tenant, cs=cs, job=job)),
                location=location,
                workplace_type=infer_workplace(location, title),
                description_text=clip(desc),
                salary_min=sal[0] if sal else None,
                salary_max=sal[1] if sal else None,
                salary_currency=sal[2] if sal else None,
                seniority=infer_seniority(title),
                posted_at=posted,
            )
        )
    return out


def _int(v) -> int:
    try:
        return max(int(v), 0)
    except (TypeError, ValueError):
        return 0


def _body(page: int) -> dict:
    return {
        "multilineEnabled": False,
        "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
        "fieldData": {"fields": {"KEYWORD": "", "LOCATION": ""}, "valid": True},
        "filterSelectionParam": {"searchFilterSelections": []},
        "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []},
        "pageNo": page,
    }


async def _get_page(robots, url: str, tenant: str) -> httpx.Response:
    """GET a career-section page, following redirects only while they stay on {tenant}.taleo.net
    (each hop robots-checked); anything else is treated as the section being gone."""
    for _ in range(MAX_REDIRECTS + 1):
        try:
            return await robots.get(url, headers=PAGE_HEADERS, follow_redirects=False)
        except httpx.HTTPStatusError as e:  # PoliteFetcher.get raises on 3xx too
            if not e.response.is_redirect:
                raise
            r = e.response
        nxt = r.url.join(r.headers.get("location", ""))
        if nxt.scheme != "https" or nxt.host != f"{tenant}.taleo.net":
            raise CareerSectionUnavailable(f"taleo career section on '{tenant}' redirects off the tenant host")
        url = str(nxt)
    raise CareerSectionUnavailable(f"too many redirects on taleo tenant '{tenant}'")


async def _portal(robots, tenant: str, cs: str) -> str:
    """The career section's portal id, read from its search page (which may redirect, e.g. to moresearch.ftl)."""
    page = (await _get_page(robots, SEARCH_PAGE.format(tenant=tenant, cs=cs), tenant)).text
    if _UNAVAILABLE_RE.search(page[:5000]):
        raise CareerSectionUnavailable(f"taleo career section '{tenant}/{cs}' is unavailable")
    portal = extract_portal(page, cs)
    if not portal:
        raise CareerSectionUnavailable(f"no portal id on the taleo career section page '{tenant}/{cs}'")
    return portal


async def fetch_taleo(
    client: httpx.AsyncClient, slug: str, company_name: str, list_only: bool = False
) -> list[JobPosting]:
    """list_only fetches the first listing page and no job pages (postings then have no description)."""
    tenant, cs = parse_slug(slug)  # the host is always {tenant}.taleo.net, never taken from the slug as-is
    robots = PoliteFetcher(client)
    portal = await _portal(robots, tenant, cs)
    url = LIST_API.format(tenant=tenant, portal=portal)
    listed: list[dict] = []
    seen: set[str] = set()
    for page in range(1, 2 if list_only else MAX_PAGES + 1):
        try:
            data = (await robots.post(url, json=_body(page), headers=API_HEADERS)).json()
            if not isinstance(data, dict):
                raise TypeError("searchjobs did not return an object")
            if data.get("careerSectionUnAvailable"):
                raise CareerSectionUnavailable(f"taleo career section '{tenant}/{cs}' is unavailable")
        except (httpx.HTTPError, ValueError, TypeError):
            if page > 1:  # a later page failing: keep what we have
                break
            raise
        reqs = [r for r in (data.get("requisitionList") or []) if isinstance(r, dict)]
        fresh = []
        for r in reqs:
            job = _job_no(r)
            if job and job not in seen:
                seen.add(job)
                fresh.append(r)
        listed.extend(fresh)
        paging = data.get("pagingData") if isinstance(data.get("pagingData"), dict) else {}
        total, size = _int(paging.get("totalCount")), _int(paging.get("pageSize")) or PAGE
        if not fresh or (total and page * size >= total) or (not total and len(reqs) < size):
            break

    listed = [r for r in listed if _columns(r)[0]]  # only postings parse_taleo keeps are worth a page fetch
    sample = JOB_URL.format(tenant=tenant, cs=cs, job="1")
    if list_only or not listed or not await robots.allowed(sample):
        return parse_taleo(listed, company_name, slug)

    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
    failed = 0

    async def detail(item: dict) -> dict:
        nonlocal failed
        async with sem:
            try:
                job_url = JOB_URL.format(tenant=tenant, cs=cs, job=_job_no(item))
                r = await _get_page(robots, job_url, tenant)
                return {**item, "description": job_description(r.text)}
            except Exception:  # HTTP error / robots / odd page: keep the listing data
                failed += 1
                return item

    head, tail = listed[:MAX_DETAILS], listed[MAX_DETAILS:]
    details = list(await asyncio.gather(*(detail(i) for i in head)))
    if len(head) >= 3 and failed == len(head):
        raise RuntimeError(f"all {failed} job page requests failed")
    return parse_taleo(details + tail, company_name, slug)
