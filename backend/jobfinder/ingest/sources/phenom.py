"""Phenom People career sites. Always on the company's own domain (careers.mastercard.com, jobs.thermofisher.com),
so boards are recognised from page HTML (see detect_phenom) rather than from the URL.

Slug format: "{host}__{country}__{lang}", e.g. "careers.mastercard.com__us__en" for careers.mastercard.com/us/en/.
"""
import asyncio
import html as htmllib
import json
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

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

LIST_API = "https://{host}/widgets"
JOB_URL = "https://{host}/{country}/{lang}/job/{job_id}"
PAGE = 100
MAX_PAGES = 20  # 2000 postings per board
MAX_DETAILS = 200  # per-board cap on job-page fetches; the rest keep the listing teaser
DETAIL_CONCURRENCY = 4
PAGE_MAX_BYTES = 4_000_000  # job pages inline a ~1 MB app state blob

_HOST_RE = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_LOCALE_RE = re.compile(r"^[a-z]{2,6}$")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_LD_RE = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
_MARKER_RE = re.compile(r"phApp\.|\"refNum\"\s*:")


def parse_slug(slug: str) -> tuple[str, str, str]:
    """(host, country, lang) from "{host}__{country}__{lang}"; ValueError when malformed."""
    parts = (slug or "").split("__")
    if len(parts) != 3 or not (_HOST_RE.match(parts[0]) and _LOCALE_RE.match(parts[1]) and _LOCALE_RE.match(parts[2])):
        raise ValueError(f"invalid phenom slug '{slug}' (expected host__country__lang)")
    return parts[0], parts[1], parts[2]


def make_slug(host: str, country: str, lang: str) -> str | None:
    slug = f"{(host or '').lower()}__{(country or '').lower()}__{(lang or '').lower()}"
    try:
        parse_slug(slug)
    except ValueError:
        return None
    return slug


def detect_phenom(page_url: str, html: str) -> tuple[str, str] | None:
    """A Phenom page loads assets from phenompeople.com and inlines its `phApp` state (with a "refNum").
    The board is the page's host plus the /{country}/{lang}/ locale from its baseUrl (or else its path)."""
    if "phenompeople" not in html or not _MARKER_RE.search(html):
        return None
    parts = urlsplit(page_url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not _HOST_RE.match(host):
        return None
    # the site's own baseUrl ("https://{host}/us/en/") is authoritative; a baseUrl for another host is ignored
    m = re.search(r"\"baseUrl\"\s*:\s*\"https?://" + re.escape(host) + r"/([a-z]{2,6})/([a-z]{2,6})(?:/[^\"]*)?\"",
                  html, re.IGNORECASE)
    segs = [s for s in parts.path.lower().split("/") if s]
    if m:
        slug = make_slug(host, m.group(1), m.group(2))
    elif len(segs) >= 2 and _LOCALE_RE.match(segs[0]) and _LOCALE_RE.match(segs[1]):
        slug = make_slug(host, segs[0], segs[1])
    else:
        slug = None
    return ("phenom", slug) if slug else None


def _dt(s) -> datetime | None:
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        d = datetime.fromisoformat(s.strip())
    except ValueError:
        return None
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)


def _location(j: dict) -> str:
    multi = [m.strip() for m in (j.get("multi_location") or []) if isinstance(m, str) and m.strip()]
    if len(multi) > 1:
        return "; ".join(multi)[:500]
    return str(j.get("cityStateCountry") or j.get("location") or j.get("cityState") or (multi[0] if multi else "")).strip()


def job_posting_ld(page_html: str) -> dict:
    """The schema.org JobPosting embedded in a job page ({} when absent or unparsable)."""
    for m in _LD_RE.finditer(page_html):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        for node in data if isinstance(data, list) else (data.get("@graph") or [data]) if isinstance(data, dict) else []:
            types = node.get("@type") if isinstance(node, dict) else None
            if types == "JobPosting" or (isinstance(types, list) and "JobPosting" in types):
                return node
    return {}


def _ld_description(ld: dict) -> str:
    # Phenom entity-escapes the HTML inside the JSON-LD ("&lt;p&gt;..."); unescape once, then strip tags
    desc = ld.get("description")
    return html_to_text(htmllib.unescape(desc)) if isinstance(desc, str) else ""


def _job_id(j: dict) -> str:
    return str(j.get("jobId") or j.get("reqId") or "").strip()


def _keepable(j: dict) -> bool:
    return bool(str(j.get("title") or "").strip()) and bool(_JOB_ID_RE.match(_job_id(j)))


def parse_phenom(jobs: list[dict], company_name: str, slug: str) -> list[JobPosting]:
    """`jobs` are refineSearch listing entries; a "detail" key may hold the job page's JSON-LD JobPosting."""
    host, country, lang = parse_slug(slug)
    out = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        ld = j.get("detail") or {}
        title = str(j.get("title") or "").strip()
        job_id = _job_id(j)
        if not _keepable(j):
            continue
        location = _location(j)
        loc_type = ld.get("jobLocationType")
        remote = "TELECOMMUTE" in [str(t).upper() for t in (loc_type if isinstance(loc_type, list) else [loc_type])]
        desc = _ld_description(ld) or html_to_text(str(j.get("descriptionTeaser") or ""))
        sal = parse_salary_range(desc)
        out.append(
            JobPosting(
                source="phenom",
                external_id=f"{host}:{job_id}",  # req ids are only unique within a tenant
                company_name=company_name,
                title=title,
                # the applyUrl usually points at the backing ATS's apply form; the Phenom job page is the posting
                url=safe_http_url(JOB_URL.format(host=host, country=country, lang=lang, job_id=job_id)),
                location=location,
                workplace_type=infer_workplace(location, title, declared="remote" if remote else None),
                employment_type=str(j.get("type") or "").strip() or None,
                description_text=clip(desc),
                salary_min=sal[0] if sal else None,
                salary_max=sal[1] if sal else None,
                salary_currency=sal[2] if sal else None,
                seniority=infer_seniority(title),
                posted_at=_dt(j.get("postedDate")) or _dt(j.get("dateCreated")) or _dt(ld.get("datePosted")),
            )
        )
    return out


def _raise_status(url: str, status: int, method: str = "GET") -> None:
    """safe_fetch reports HTTP errors as FetchError; ingest's retry/health logic keys on HTTPStatusError."""
    if status >= 400:
        req = httpx.Request(method, url)
        raise httpx.HTTPStatusError(f"HTTP {status} from {url}", request=req, response=httpx.Response(status, request=req))


def _body(country: str, lang: str, offset: int) -> dict:
    return {
        "lang": f"{lang}_{country}", "deviceType": "desktop", "country": country, "pageName": "search-results",
        "ddoKey": "refineSearch", "sortBy": "Most recent", "from": offset, "size": PAGE, "jobs": True,
        "counts": False, "all_fields": [], "siteType": "external", "keywords": "", "global": True,
        "selected_fields": {},
    }


async def fetch_phenom(
    client: httpx.AsyncClient, slug: str, company_name: str, list_only: bool = False
) -> list[JobPosting]:
    """list_only fetches just the first listing page (descriptions are the listing teasers) and no job pages."""
    # imported here: discovery imports the source registry, which imports this module
    from jobfinder.ingest.discovery.robots import PoliteFetcher
    from jobfinder.ingest.http_safety import (
        JSON_TYPES,
        TEXT_TYPES,
        FetchError,
        assert_public_host,
        safe_fetch,
    )

    host, country, lang = parse_slug(slug)
    await assert_public_host(host)  # the host comes from a slug a user may have pasted
    robots = PoliteFetcher(client)
    url = LIST_API.format(host=host)
    listed: list[dict] = []
    seen: set[str] = set()
    offset = 0
    for page in range(1 if list_only else MAX_PAGES):
        try:
            r = await safe_fetch(client, url, method="POST", json=_body(country, lang, offset),
                                 headers={"Accept": "application/json"}, accept=JSON_TYPES, robots=robots,
                                 raise_for_status=False)
            _raise_status(url, r.status, "POST")
            data = r.json()
            if not isinstance(data, dict) or not isinstance(data.get("refineSearch"), dict):
                raise TypeError("no refineSearch object in the /widgets response")
        except (httpx.HTTPStatusError, FetchError, ValueError, TypeError):
            if page > 0:  # a later page failing (offset cap, timeout): keep what we have
                break
            raise
        search = data["refineSearch"]
        jobs = [j for j in ((search.get("data") or {}).get("jobs") or []) if isinstance(j, dict)]
        fresh = [j for j in jobs if _job_id(j) not in seen]
        seen.update(_job_id(j) for j in fresh)
        listed.extend(fresh)
        offset += len(jobs)  # some tenants cap `size` below what we ask for
        total = search.get("totalHits") or 0
        if not fresh or (total and offset >= total) or (not total and len(jobs) < PAGE):
            break

    listed = [j for j in listed if _keepable(j)]  # only postings parse_phenom keeps are worth a page fetch
    sample = JOB_URL.format(host=host, country=country, lang=lang, job_id="x")
    if list_only or not listed or not await robots.allowed(sample):  # job pages off limits: keep the teasers
        return parse_phenom(listed, company_name, slug)

    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
    failed = 0

    async def detail(item: dict) -> dict:
        nonlocal failed
        job_id = _job_id(item)
        async with sem:
            try:
                page_url = JOB_URL.format(host=host, country=country, lang=lang, job_id=job_id)
                r = await safe_fetch(client, page_url, accept=TEXT_TYPES, robots=robots, max_bytes=PAGE_MAX_BYTES)
                return {**item, "detail": job_posting_ld(r.text)}
            except Exception:  # FetchError / robots / bad page: keep the listing data (teaser description)
                failed += 1
                return item

    head, tail = listed[:MAX_DETAILS], listed[MAX_DETAILS:]
    details = list(await asyncio.gather(*(detail(i) for i in head)))
    if len(head) >= 3 and failed == len(head):
        raise RuntimeError(f"all {failed} job page requests failed")
    return parse_phenom(details + tail, company_name, slug)
