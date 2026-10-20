"""SAP SuccessFactors Recruiting Marketing (RMK, ex-jobs2web / Career Site Builder) boards.

RMK sites always live on the company's own domain (careers.ey.com/ey/...), so the board key is the host plus
an optional brand path segment: slug "careers.ey.com__ey" -> https://careers.ey.com/ey/search/. The search page
is server-rendered HTML (25 rows per page, "Results 1 – 25 of N"); each job page carries schema.org microdata
(title, description, datePosted, streetAddress). robots.txt is honoured on every request; /services/ (RSS and
the UDM API) is disallowed on these sites and is never called.
"""
import asyncio
import re
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpx
from lxml import etree
from lxml import html as lxml_html

from jobfinder.core.http import FetchError
from jobfinder.ingest.base import (
    JobPosting,
    clip,
    html_to_text,
    infer_seniority,
    infer_workplace,
    parse_salary_range,
    safe_http_url,
)
from jobfinder.ingest.http_safety import TEXT_TYPES, assert_public_host, safe_fetch
from jobfinder.integrations.ats import SLUG_RE
from jobfinder.integrations.robots import PoliteFetcher

SEARCH_URL = "{base}/search/?q=&sortColumn=referencedate&sortDirection=desc&startrow={start}"
MAX_PAGES = 40  # 25 rows a page -> 1,000 newest postings (EY lists ~8,800)
MAX_DETAILS = 200  # per-board cap on job-page fetches; the rest keep listing data only
DETAIL_CONCURRENCY = 4

_HOST_RE = re.compile(
    r"^(?=.{4,150}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_BRAND_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_RESERVED_BRANDS = {"job", "search", "services", "go", "content", "talentcommunity"}
_JOB_PATH_RE = re.compile(r"/job/[^/?#]+/(\d{1,20})/?$")
# RMK pages load assets from rmkcdn and boot the jobs2web "j2w" scripts; a page merely naming jobs2web is not one
_RMK_RE = re.compile(r"rmkcdn\.successfactors\.(?:com|eu)|rsslink-jobs2web|\bj2w\.(?:init|Apply|core)\b")
_SEARCH_ACTION_RE = re.compile(r"""action=["'](?:https?://[^/"'\s]+)?(?:/([A-Za-z0-9_-]{1,40}))?/search/""")
_TOTAL_RE = re.compile(r"<b>[^<]*</b>[^<]*<b>\s*([\d.,\s ]{1,15})</b>")
_JAVA_DATE_RE = re.compile(r"^\w{3} (\w{3}) (\d{1,2}) (\d{2}:\d{2}:\d{2}) [A-Za-z+\-0-9:]+ (\d{4})$")
_DATE_FORMATS = ("%b %d, %Y", "%d %b %Y", "%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y", "%d/%m/%Y")


def _cls(name: str) -> str:
    """XPath predicate: the element's class list contains `name` (no cssselect dependency)."""
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {name} ')"


def parse_slug(slug: str) -> tuple[str, str | None]:
    """(host, brand|None) from "host" or "host__brand"; ValueError when malformed."""
    if not SLUG_RE.match(slug or ""):
        raise ValueError(f"invalid successfactors slug '{slug}'")
    host, sep, brand = slug.partition("__")
    host = host.lower()
    if not _HOST_RE.match(host) or (sep and (not _BRAND_RE.match(brand) or brand.lower() in _RESERVED_BRANDS)):
        raise ValueError(f"invalid successfactors slug '{slug}' (expected host or host__brand)")
    return host, brand or None


def make_slug(host: str, brand: str | None = None) -> str:
    slug = f"{host.lower()}__{brand}" if brand else host.lower()
    parse_slug(slug)  # raises when either part is malformed
    return slug


def board_base(host: str, brand: str | None) -> str:
    return f"https://{host}/{brand}" if brand else f"https://{host}"


def _job_id(url: str, host: str) -> str | None:
    """The numeric job id of an on-board job URL ({base}/job/{City-Title-Zip}/{id}/), else None."""
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != host:
        return None
    m = _JOB_PATH_RE.search(parts.path)
    return m.group(1) if m else None


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.text_content()).strip() if el is not None else ""


def _first(nodes):
    return nodes[0] if nodes else None


def parse_date(raw: str | None) -> datetime | None:
    """"Tue Oct 13 02:00:00 UTC 2026" (microdata) or a listing date ("Oct 13, 2026", "13 Oct 2026") -> UTC."""
    raw = re.sub(r"\s+", " ", raw or "").strip()
    if not raw:
        return None
    m = _JAVA_DATE_RE.match(raw)
    if m:  # the zone is nearly always UTC/GMT; treating it as UTC is off by hours at most
        raw = f"{m.group(1)} {m.group(2)} {m.group(4)} {m.group(3)}"
        try:
            return datetime.strptime(raw, "%b %d %Y %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def parse_total(page_html: str) -> int | None:
    """N from the "Results 1 – 25 of N" pagination label."""
    i = page_html.find("paginationLabel")
    m = _TOTAL_RE.search(page_html, i, i + 400) if i >= 0 else None
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def parse_search(page_html: str, host: str, base: str) -> list[dict]:
    """Rows of one search page -> [{id, title, url, location, posted}]. Rows without an on-board job URL
    (e.g. a javascript: link) or a title are dropped."""
    if not page_html.strip():
        return []
    doc = lxml_html.fromstring(page_html)
    rows, seen = [], set()
    for tr in doc.xpath(f"//tr[{_cls('data-row')}]"):
        a = _first(tr.xpath(f".//a[{_cls('jobTitle-link')}]"))
        if a is None:
            continue
        url = safe_http_url(urljoin(base + "/", (a.get("href") or "").strip()))
        jid = _job_id(url, host) if url else None
        title = _text(a)
        if not jid or not title or jid in seen:
            continue
        seen.add(jid)
        rows.append({
            "id": jid,
            "title": title,
            "url": url,
            "location": _text(_first(tr.xpath(f".//span[{_cls('jobLocation')}]"))),
            "posted": _text(_first(tr.xpath(f".//span[{_cls('jobDate')}]"))),
        })
    return rows


def parse_detail(page_html: str) -> dict:
    """Microdata of a job page -> {title, description (html), location, posted}; missing keys are omitted."""
    doc = lxml_html.fromstring(page_html)
    out = {}
    title = _first(doc.xpath("//*[@itemprop='title']"))
    if title is not None and _text(title):
        out["title"] = _text(title)
    desc = _first(doc.xpath("//*[@itemprop='description'][not(self::meta)]"))
    if desc is not None:
        out["description"] = lxml_html.tostring(desc, encoding="unicode")
    street = _first(doc.xpath("//*[@itemprop='streetAddress']/@content"))
    if street and street.strip():
        out["location"] = street.strip()
    posted = _first(doc.xpath("//*[@itemprop='datePosted']/@content"))
    if posted:
        out["posted"] = posted
    return out


def parse_successfactors(rows: list[dict], company_name: str, slug: str) -> list[JobPosting]:
    """`rows` are parse_search rows; a "detail" key may hold parse_detail output."""
    host, _ = parse_slug(slug)
    out = []
    for r in rows:
        d = r.get("detail") or {}
        title = (d.get("title") or r.get("title") or "").strip()
        url = safe_http_url(r.get("url"))
        if not r.get("id") or not title or not url:
            continue
        location = (d.get("location") or r.get("location") or "").strip()
        desc = html_to_text(d.get("description") or "")
        sal = parse_salary_range(desc) or (None, None, None)
        out.append(
            JobPosting(
                source="successfactors",
                external_id=f"{host}:{r['id']}",  # RMK ids are per SuccessFactors instance; the host keeps them apart
                company_name=company_name,
                title=title,
                url=url,
                location=location,
                workplace_type=infer_workplace(location, title),
                description_text=clip(desc),
                salary_min=sal[0],
                salary_max=sal[1],
                salary_currency=sal[2],
                seniority=infer_seniority(title),
                posted_at=parse_date(d.get("posted")) or parse_date(r.get("posted")),
            )
        )
    return out


async def _get(client: httpx.AsyncClient, robots: PoliteFetcher, url: str) -> str:
    """Robots-checked, SSRF-safe GET of an HTML page; HTTP errors surface as httpx.HTTPStatusError so ingest
    health treats them like any other ATS."""
    r = await safe_fetch(client, url, accept=TEXT_TYPES, robots=robots, raise_for_status=False)
    if r.status >= 400:
        req = httpx.Request("GET", url)
        raise httpx.HTTPStatusError(f"HTTP {r.status} from {url}", request=req,
                                    response=httpx.Response(r.status, request=req))
    return r.text


async def fetch_successfactors(
    client: httpx.AsyncClient, slug: str, company_name: str, list_only: bool = False
) -> list[JobPosting]:
    """list_only reads just the first search page and skips the job-page fetches (cheap probe)."""
    host, brand = parse_slug(slug)
    await assert_public_host(host)  # a custom domain from the slug: never let it point inside
    base = board_base(host, brand)
    robots = PoliteFetcher(client)  # loads robots.txt through safe_fetch, so it is IP-pinned too

    listed: list[dict] = []
    seen: set[str] = set()
    total, start = None, 0
    for page in range(1 if list_only else MAX_PAGES):
        body = await _get(client, robots, SEARCH_URL.format(base=base, start=start))
        rows = await asyncio.to_thread(parse_search, body, host, base)
        if page == 0:
            total = parse_total(body)
        fresh = [r for r in rows if r["id"] not in seen]
        seen.update(r["id"] for r in fresh)
        listed.extend(fresh)
        start += len(rows)
        if not fresh or (total is not None and start >= total):
            break

    if list_only:
        return await asyncio.to_thread(parse_successfactors, listed, company_name, slug)

    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
    failed = 0

    async def detail(row: dict) -> dict:
        nonlocal failed
        async with sem:
            try:
                page = await _get(client, robots, row["url"])
                return {**row, "detail": await asyncio.to_thread(parse_detail, page)}
            except (FetchError, httpx.HTTPError, ValueError, etree.LxmlError):
                failed += 1  # keep the listing data; the upsert preserves any stored description
                return row

    head, tail = listed[:MAX_DETAILS], listed[MAX_DETAILS:]
    details = list(await asyncio.gather(*(detail(r) for r in head)))
    if len(head) >= 3 and failed == len(head):
        raise RuntimeError(f"all {failed} job page requests failed")
    return await asyncio.to_thread(parse_successfactors, details + tail, company_name, slug)


def detect_successfactors(page_url: str, page_html: str) -> tuple[str, str] | None:
    """HTML detector: an RMK page (assets from rmkcdn / jobs2web markup) -> ("successfactors", host[__brand]).
    The brand is the path segment before /job/ or /search/, else the one in the page's search form."""
    if not _RMK_RE.search(page_html or ""):
        return None
    parts = urlsplit(page_url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not _HOST_RE.match(host):
        return None
    segs = [s for s in parts.path.split("/") if s]
    i = next((k for k, seg in enumerate(segs) if seg in ("job", "search")), -1)
    if i in (0, 1):
        brand = segs[0] if i == 1 else None
    else:  # a landing page (or a deeper path): the search form's action names the brand
        m = _SEARCH_ACTION_RE.search(page_html)
        brand = m.group(1) if m else None
    try:
        return "successfactors", make_slug(host, brand)
    except ValueError:
        return None
