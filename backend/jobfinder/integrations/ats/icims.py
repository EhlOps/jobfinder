"""iCIMS classic portals: {prefix}-{tenant}.icims.com (e.g. careers-scsengineers.icims.com).

There is no public JSON API, so this scrapes the portal's own HTML/XML (robots.txt honoured, every hop through
safe_fetch). The job list comes from the paged search (/jobs/search?ss=1&in_iframe=1&pr=N, "Page X of Y"),
whose cards carry the real title and location; if a tenant's search is empty or blocked we fall back to
/sitemap.xml (every open job URL; titles then come from the URL slug). Each posting's detail page
(?in_iframe=1; without it you get a wrapper page) carries a schema.org JobPosting in JSON-LD with the
description, date and salary. Many tenants redirect to their own site or a Jibe front end instead: a board
whose pages land off *.icims.com is not an iCIMS board and is refused before the redirect is followed.

Slug: the portal's full first DNS label, e.g. "careers-scsengineers".
"""
import asyncio
import html as htmllib
import json
import re
from datetime import UTC, datetime
from urllib.parse import unquote, urljoin, urlsplit

import httpx
import lxml.etree
import lxml.html

from jobfinder.ingest.base import (
    JobPosting,
    clip,
    html_to_text,
    infer_seniority,
    infer_workplace,
    parse_salary_range,
    safe_http_url,
)
from jobfinder.ingest.http_safety import TEXT_TYPES, FetchError, safe_fetch
from jobfinder.integrations.robots import PoliteFetcher

SUFFIX = ".icims.com"
SITEMAP = "https://{host}/sitemap.xml"
SEARCH = "https://{host}/jobs/search?ss=1&in_iframe=1&pr={page}"
DETAIL = "https://{host}/jobs/{id}/{path}/job?in_iframe=1"
JOB_URL = "https://{host}/jobs/{id}/{path}/job"
MAX_PAGES = 50
MAX_DETAILS = 300  # per-board cap on detail-page fetches; the rest keep their listing data
DETAIL_CONCURRENCY = 4
SITEMAP_MAX_BYTES = 5_000_000

# Tenant portals are always "{prefix}-{tenant}"; requiring the hyphen is what keeps out iCIMS's own hosts
# (www, login, developer, ...), which never belong to a hiring company.
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_ID_RE = re.compile(r"^\d{1,12}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9%._~-]{0,300}$")
_PAGES_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.IGNORECASE)
_LD_RE = re.compile(
    r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL
)
_URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.DOTALL)
_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.DOTALL)
# an embedded portal link: https://careers-acme.icims.com/jobs... (or JSON-escaped: https:\/\/...\/jobs)
_EMBED_RE = re.compile(
    r"https?:(?://|\\/\\/)([a-z0-9][a-z0-9-]{1,62})\.icims\.com(?:/|\\/)jobs\b", re.IGNORECASE
)


class NotIcimsBoard(RuntimeError):
    """The portal redirects off *.icims.com (to the company's own site or a Jibe front end)."""


def valid_label(label: str) -> bool:
    return bool(LABEL_RE.match(label)) and "-" in label and not label.endswith("-")


def parse_slug(slug: str) -> str:
    """The portal host for a slug ("careers-acme" -> "careers-acme.icims.com"); ValueError when malformed."""
    label = (slug or "").strip().lower()
    if not valid_label(label):
        raise ValueError(f"invalid icims slug '{slug}' (expected the portal label, e.g. careers-acme)")
    return label + SUFFIX


def slug_from_host(host: str) -> str | None:
    """"careers-acme.icims.com" -> "careers-acme"; None for anything that isn't a tenant portal."""
    host = (host or "").lower().rstrip(".")
    if not host.endswith(SUFFIX):
        return None
    label = host[: -len(SUFFIX)]
    return label if valid_label(label) else None


def detect_icims(page_url: str, html: str) -> tuple[str, str] | None:
    """A custom careers page that embeds (iframe/script/link) an iCIMS portal's /jobs pages -> ("icims", label).
    Only /jobs links count, so asset hosts and "powered by iCIMS" links are ignored."""
    for m in _EMBED_RE.finditer(html or ""):
        label = m.group(1).lower()
        if valid_label(label):
            return "icims", label
    return None


def _safe_path(path: str) -> bool:
    """The title slug in a job URL: one plain path segment (no traversal, no encoded separators)."""
    low = path.lower()
    return bool(_PATH_RE.match(path)) and ".." not in path and not any(e in low for e in ("%2e", "%2f", "%5c"))


def _job_ref(url: str, host: str) -> tuple[str, str] | None:
    """(job id, url path slug) from a job URL on this portal, or None (other hosts and paths are ignored)."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() != host:
        return None
    segs = parts.path.split("/")
    # ["", "jobs", id, slug, "job"]
    if len(segs) != 5 or segs[1] != "jobs" or segs[4] != "job":
        return None
    if not _ID_RE.match(segs[2]) or not _safe_path(segs[3]):
        return None
    return segs[2], segs[3]


def title_from_path(path: str) -> str:
    """"senior-hydrogeologist---groundwater" -> "Senior Hydrogeologist - Groundwater" (listing-only fallback)."""
    words = unquote(path).replace("---", " – ").replace("-", " ").replace("–", "-")
    return " ".join(w if w.isupper() else w.capitalize() for w in words.split())


def _dt(s) -> datetime | None:
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        d = datetime.fromisoformat(s.strip())  # 3.11 accepts a trailing Z
    except ValueError:
        return None
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)


# ── parsers (pure) ───────────────────────────────────────────────────────
def parse_sitemap(xml: str, host: str) -> list[dict]:
    """Job entries ({id, path}) from the portal's sitemap, in order, without duplicates."""
    out: dict[str, dict] = {}
    for block in _URL_BLOCK_RE.findall(xml or ""):
        loc = _LOC_RE.search(block)
        ref = _job_ref(htmllib.unescape(loc.group(1)), host) if loc else None
        if ref and ref[0] not in out:
            out[ref[0]] = {"id": ref[0], "path": ref[1]}
    return list(out.values())


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.text_content()).strip() if el is not None else ""


def parse_search_page(page_html: str, host: str) -> tuple[list[dict], int]:
    """(cards, total pages) from one search results page. Cards: {id, path, title, location}."""
    try:  # bytes, so an XHTML encoding declaration is accepted
        doc = lxml.html.fromstring((page_html or "").encode("utf-8"))
    except (lxml.etree.ParserError, ValueError):  # empty or comment-only document
        return [], 0
    base = f"https://{host}/jobs/search"
    cards = []
    for li in doc.xpath("//li[contains(concat(' ', normalize-space(@class), ' '), ' iCIMS_JobCardItem ')]"):
        a = next(iter(li.xpath(".//div[contains(@class, 'title')]//a[@href]")), None)
        ref = _job_ref(urljoin(base, a.get("href", "").strip()), host) if a is not None else None
        if not ref:
            continue
        h3 = next(iter(a.xpath(".//h3")), None)
        loc = li.xpath(".//div[contains(@class, 'header') and contains(@class, 'left')]"
                       "//span[not(contains(@class, 'sr-only'))]")
        cards.append({"id": ref[0], "path": ref[1], "title": _text(h3), "location": _text(loc[0]) if loc else ""})
    m = _PAGES_RE.search(doc.text_content())
    return cards, int(m.group(2)) if m else 1


def _postings_in(node) -> list[dict]:
    """Every JobPosting object in a JSON-LD value (object, list or @graph)."""
    if isinstance(node, list):
        return [p for n in node for p in _postings_in(n)]
    if not isinstance(node, dict):
        return []
    t = node.get("@type")
    if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
        return [node]
    return _postings_in(node.get("@graph") or [])


def parse_jsonld(page_html: str) -> dict | None:
    """The first schema.org JobPosting on a detail page, or None."""
    for block in _LD_RE.findall(page_html or ""):
        try:
            data = json.loads(block.strip(), strict=False)  # tenants paste raw newlines into descriptions
        except ValueError:
            continue
        if found := _postings_in(data):
            return found[0]
    return None


def _location(ld: dict) -> str:
    locs = ld.get("jobLocation")
    out = []
    for loc in locs if isinstance(locs, list) else [locs]:
        addr = loc.get("address") if isinstance(loc, dict) else None
        if isinstance(addr, dict):
            bits = [addr.get(k) for k in ("addressLocality", "addressRegion", "addressCountry")]
            text = ", ".join(str(b).strip() for b in bits if isinstance(b, str | int) and str(b).strip())
            if text and text not in out:
                out.append(text)
    return "; ".join(out)


def _salary(ld: dict) -> tuple[int, int, str] | None:
    """Annual salary from baseSalary (min/max on the object or under `value`); hourly etc. are skipped."""
    bs = ld.get("baseSalary")
    if not isinstance(bs, dict):
        return None
    val = bs.get("value") if isinstance(bs.get("value"), dict) else bs
    unit = str(val.get("unitText") or bs.get("unitText") or "YEAR").upper()
    try:
        lo, hi = int(float(val.get("minValue"))), int(float(val.get("maxValue")))
    except (TypeError, ValueError, OverflowError):
        return None
    # tenants often omit unitText on hourly pay ({"minValue": 23, "maxValue": 25}), so also demand an
    # annual-looking band, as parse_salary_range does
    if unit != "YEAR" or not (20_000 <= lo <= hi <= 1_500_000):
        return None
    cur = bs.get("currency") or ld.get("salaryCurrency") or "USD"
    return lo, hi, str(cur)[:3].upper()


def _employment(ld: dict) -> str | None:
    et = ld.get("employmentType")
    et = et[0] if isinstance(et, list) and et else et
    return str(et).replace("_", " ").capitalize() if isinstance(et, str) and et.strip() else None


def parse_icims(items: list[dict], company_name: str, slug: str) -> list[JobPosting]:
    """`items` are listing entries ({id, path, title?, location?}); a "detail" key may hold the posting's
    JSON-LD JobPosting. Without one the description stays empty (not the card's teaser), so the upsert keeps
    any description and salary stored from an earlier run."""
    host = parse_slug(slug)
    label = host[: -len(SUFFIX)]
    out = []
    for it in items:
        jid, path = str(it.get("id") or ""), str(it.get("path") or "")
        if not _ID_RE.match(jid) or not _safe_path(path):
            continue
        ld = it.get("detail") or {}
        title = html_to_text(str(ld.get("title") or "")) or it.get("title") or title_from_path(path)
        title = title.strip()
        if not title:
            continue
        location = _location(ld) or (it.get("location") or "").strip()
        desc = html_to_text(ld["description"]) if isinstance(ld.get("description"), str) else ""
        sal = _salary(ld) or parse_salary_range(desc) or (None, None, None)
        remote = "remote" if str(ld.get("jobLocationType") or "").upper() == "TELECOMMUTE" else None
        out.append(
            JobPosting(
                source="icims",
                external_id=f"{label}:{jid}",  # job ids are per portal
                company_name=company_name,
                title=title[:500],
                url=safe_http_url(JOB_URL.format(host=host, id=jid, path=path)),
                location=location,
                workplace_type=infer_workplace(location, title, declared=remote),
                employment_type=_employment(ld),
                description_text=clip(desc),
                salary_min=sal[0],
                salary_max=sal[1],
                salary_currency=sal[2],
                seniority=infer_seniority(title),
                posted_at=_dt(ld.get("datePosted")),
            )
        )
    return out


# ── fetching ─────────────────────────────────────────────────────────────
class _PortalRobots:
    """robots.txt for the portal, consulted by safe_fetch before every hop. A hop off *.icims.com (the
    tenant redirects to its own site or a Jibe front end) is refused here, before anything is fetched there."""

    def __init__(self, fetcher):
        self._fetcher = fetcher

    async def allowed(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        if not host.endswith(SUFFIX):
            raise NotIcimsBoard(f"the portal redirects to {host or 'elsewhere'}; not an iCIMS board")
        return await self._fetcher.allowed(url)


def _transport(client) -> tuple[httpx.AsyncClient, _PortalRobots]:
    # discovery probes pass a robots-checking wrapper (get/post only); fetch through its real client instead
    fetcher = getattr(client, "_fetcher", None)
    if isinstance(fetcher, PoliteFetcher):
        return fetcher.client, _PortalRobots(fetcher)
    return client, _PortalRobots(PoliteFetcher(client))


async def _get(client: httpx.AsyncClient, url: str, robots: _PortalRobots, max_bytes: int = 2_000_000) -> str:
    """Fetch one portal page. HTTP errors surface as httpx.HTTPStatusError (so a 404 reads as "board not
    found"); a redirect off *.icims.com raises NotIcimsBoard; robots/SSRF refusals raise FetchError."""
    r = await safe_fetch(client, url, robots=robots, accept=TEXT_TYPES, max_bytes=max_bytes,  # type: ignore[arg-type]
                         raise_for_status=False)
    if r.status >= 400:
        req = httpx.Request("GET", url)
        raise httpx.HTTPStatusError(f"HTTP {r.status} from {url}", request=req,
                                    response=httpx.Response(r.status, request=req))
    return r.text


async def _list_search(client, host: str, robots, list_only: bool) -> list[dict]:
    cards: dict[str, dict] = {}
    for page in range(1 if list_only else MAX_PAGES):
        found, pages = parse_search_page(await _get(client, SEARCH.format(host=host, page=page), robots), host)
        new = [c for c in found if c["id"] not in cards]
        cards.update((c["id"], c) for c in new)
        if not new or page + 1 >= pages:
            break
    return list(cards.values())


async def fetch_icims(
    client: httpx.AsyncClient, slug: str, company_name: str, list_only: bool = False
) -> list[JobPosting]:
    """list_only reads just the job list (first search page, or the sitemap) and skips the detail pages."""
    host = parse_slug(slug)
    client, robots = _transport(client)
    try:
        listed = await _list_search(client, host, robots, list_only)
    except (httpx.HTTPError, FetchError) as e:  # search disabled or blocked: the sitemap may still list jobs
        search_error: Exception | None = e
        listed = []
    else:
        search_error = None
    if not listed:
        try:
            listed = parse_sitemap(await _get(client, SITEMAP.format(host=host), robots, SITEMAP_MAX_BYTES), host)
        except (httpx.HTTPError, FetchError):
            if search_error is not None:
                raise search_error from None
            raise
    if list_only:
        return parse_icims(listed, company_name, slug)

    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
    failed = 0

    async def detail(item: dict) -> dict:
        nonlocal failed
        async with sem:
            try:
                page = await _get(client, DETAIL.format(host=host, id=item["id"], path=item["path"]), robots)
            except (httpx.HTTPError, FetchError, NotIcimsBoard):
                failed += 1  # keep the listing data; the upsert preserves any stored description
                return item
        ld = parse_jsonld(page)
        return {**item, "detail": ld} if ld else item

    head, tail = listed[:MAX_DETAILS], listed[MAX_DETAILS:]
    details = list(await asyncio.gather(*(detail(i) for i in head)))
    if len(head) >= 3 and failed == len(head):
        raise RuntimeError(f"all {failed} iCIMS job page requests failed")
    return parse_icims(details + tail, company_name, slug)
