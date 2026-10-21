"""Oracle Recruiting Cloud ("Candidate Experience") career sites, via the public hcmRestApi the CE app uses.

Slug format: "{pod}.fa.{region}__{siteNumber}", e.g. "eeho.fa.us2__CX_45001" for
https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/. The host is always
"{pod}.fa.{region}.oraclecloud.com", so a slug can only ever point at Oracle's own cloud.
"""
import asyncio
import re
from datetime import UTC, datetime

import httpx

from jobfinder.core.urls import safe_http_url
from jobfinder.models.posting import (
    JobPosting,
    clip,
    html_to_text,
    infer_seniority,
    infer_workplace,
    parse_salary_range,
)

API = "https://{host}/hcmRestApi/resources/latest"
# The finder's arguments are ";"/","-separated inside one query value; the query string is built here
# (not via params=) so httpx keeps them literal. Every interpolated part is validated first.
LIST_URL = (
    API + "/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations"
    "&finder=findReqs;siteNumber={site},limit={limit},offset={offset},sortBy=POSTING_DATES_DESC"
)
DETAIL_URL = (
    API + "/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
    "&finder=ById;Id=%22{id}%22,siteNumber={site}"
)
APPLY_URL = "https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{id}"
PAGE = 50
MAX_PAGES = 100  # 5000 listed postings; paging also stops at TotalJobsCount and on an empty page
MAX_DETAILS = 300  # per-board cap on description fetches; the rest keep the listing's short description
DETAIL_CONCURRENCY = 6
MAX_ATTEMPTS = 3
MAX_RETRY_WAIT = 30.0

PREFIX_RE = re.compile(r"^[a-z0-9-]+\.fa\.[a-z0-9-]+$")  # "{pod}.fa.{region}"
SITE_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")  # "CX_45001", or a site alias the API accepts
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def parse_slug(slug: str) -> tuple[str, str]:
    """(host, siteNumber) from "{pod}.fa.{region}__{site}"; ValueError when malformed."""
    prefix, sep, site = (slug or "").partition("__")
    if not sep or not PREFIX_RE.match(prefix) or not SITE_RE.match(site):
        raise ValueError(f"invalid oracle_hcm slug '{slug}' (expected pod.fa.region__siteNumber)")
    return f"{prefix}.oraclecloud.com", site


def make_slug(host: str, site: str) -> str | None:
    """The slug for an *.oraclecloud.com host + site, or None when either part is not acceptable."""
    host = (host or "").lower()
    if not host.endswith(".oraclecloud.com"):
        return None
    prefix = host[: -len(".oraclecloud.com")]
    return f"{prefix}__{site}" if PREFIX_RE.match(prefix) and SITE_RE.match(site or "") else None


_LANG_RE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,4})?$")


def board_from_ce_url(host: str, path: str) -> tuple[str, str] | None:
    """{pod}.fa.{region}.oraclecloud.com/hcmUI/CandidateExperience/[lang/]sites/{site}/... ->
    ("oracle_hcm", "pod.fa.region__site")."""
    segs = [s for s in path.split("/") if s]
    if len(segs) < 4 or [x.lower() for x in segs[:2]] != ["hcmui", "candidateexperience"]:
        return None
    rest = segs[2:]
    if rest and _LANG_RE.match(rest[0]):
        rest = rest[1:]
    if len(rest) < 2 or rest[0].lower() != "sites":
        return None
    slug = make_slug(host, rest[1])
    return ("oracle_hcm", slug) if slug else None


_BASE_TAG_RE = re.compile(r"<base\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""\b(data-apibaseurl|data-sitenumber)\s*=\s*(["'])([^<>]{1,200}?)\2""", re.IGNORECASE)


def detect_ce_page(page_url: str, html: str) -> tuple[str, str] | None:
    """A Candidate Experience page on a custom domain (careers.oracle.com, ...) names its pod and site in
    its <base data-apibaseurl="https://{pod}.fa.{region}.oraclecloud.com:443" data-sitenumber="CX_1">."""
    for tag in _BASE_TAG_RE.findall(html or "")[:5]:
        attrs = {k.lower(): v.strip() for k, _, v in _ATTR_RE.findall(tag)}
        if "data-apibaseurl" not in attrs or "data-sitenumber" not in attrs:
            continue
        try:
            api = httpx.URL(attrs["data-apibaseurl"])
        except (httpx.InvalidURL, ValueError):
            continue
        if api.scheme != "https":
            continue
        slug = make_slug(api.host, attrs["data-sitenumber"])
        if slug:
            return "oracle_hcm", slug
    return None


def _date(value) -> datetime | None:
    """"2026-10-14" or "2026-10-14T01:03:30+00:00" -> aware UTC datetime."""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _location(item: dict) -> str:
    names = [item.get("PrimaryLocation") or ""]
    names += [(loc or {}).get("Name") or "" for loc in item.get("secondaryLocations") or []]
    seen: list[str] = []
    for n in (n.strip() for n in names if isinstance(n, str)):
        if n and n not in seen:
            seen.append(n)
    return "; ".join(seen)[:500]


def _description(detail: dict) -> str:
    """The posting body: description, then responsibilities and qualifications (each section once)."""
    parts: list[str] = []
    for key in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"):
        text = html_to_text(detail.get(key) or "") if isinstance(detail.get(key), str) else ""
        if text and text not in parts:
            parts.append(text)
    return "\n\n".join(parts)


def parse_oracle_hcm(items: list[dict], company_name: str, slug: str) -> list[JobPosting]:
    """`items` are requisitionList entries; a "detail" key may hold the RequisitionDetails item."""
    host, site = parse_slug(slug)
    prefix = host[: -len(".oraclecloud.com")]
    out = []
    for it in items:
        d = it.get("detail") or {}
        rid = str(it.get("Id") or "").strip()
        title = str(it.get("Title") or d.get("Title") or "").strip()
        if not title or not _ID_RE.match(rid):
            continue
        location = _location(it if it.get("PrimaryLocation") else d)
        desc = _description(d) or html_to_text(str(it.get("ShortDescriptionStr") or ""))
        sal = parse_salary_range(desc)
        out.append(
            JobPosting(
                source="oracle_hcm",
                external_id=f"{prefix}:{rid}",  # requisition ids are per pod
                company_name=company_name,
                title=title,
                url=safe_http_url(APPLY_URL.format(host=host, site=site, id=rid)),
                location=location,
                workplace_type=infer_workplace(
                    location, title, declared=d.get("WorkplaceType") or it.get("WorkplaceType")
                ),
                employment_type=d.get("JobSchedule") or it.get("JobSchedule") or None,
                description_text=clip(desc),
                salary_min=sal[0] if sal else None,
                salary_max=sal[1] if sal else None,
                salary_currency=sal[2] if sal else None,
                seniority=infer_seniority(title),
                posted_at=_date(it.get("PostedDate")) or _date(d.get("ExternalPostedStartDate")),
            )
        )
    return out


def _retry_wait(resp: httpx.Response, attempt: int) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", "")), MAX_RETRY_WAIT)
    except ValueError:
        return min(2.0 ** attempt, MAX_RETRY_WAIT)


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET that backs off on 429/503 and raises for any other error status."""
    for attempt in range(MAX_ATTEMPTS):
        resp = await client.get(url, headers={"Accept": "application/json"})
        if resp.status_code not in (429, 503) or attempt == MAX_ATTEMPTS - 1:
            resp.raise_for_status()
            return resp
        await asyncio.sleep(_retry_wait(resp, attempt))
    raise AssertionError("unreachable")


def _search(body) -> dict:
    items = body.get("items") if isinstance(body, dict) else None
    return items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}


async def fetch_oracle_hcm(
    client: httpx.AsyncClient, slug: str, company_name: str, list_only: bool = False
) -> list[JobPosting]:
    """list_only fetches just the first listing page and skips the per-posting detail calls (cheap probe)."""
    host, site = parse_slug(slug)
    listed: list[dict] = []
    seen: set[str] = set()
    offset = 0
    for _ in range(1 if list_only else MAX_PAGES):
        resp = await _get(client, LIST_URL.format(host=host, site=site, limit=PAGE, offset=offset))
        search = _search(resp.json())
        content = [r for r in search.get("requisitionList") or [] if isinstance(r, dict)]
        before = len(listed)
        for r in content:
            rid = str(r.get("Id") or "")
            if _ID_RE.match(rid) and str(r.get("Title") or "").strip() and rid not in seen:
                seen.add(rid)
                listed.append(r)
        offset += len(content)  # a pod may cap `limit` below PAGE; step by what it actually returned
        total = search.get("TotalJobsCount")
        total = total if isinstance(total, int) else 0
        # stop on an empty page, at the reported total, or when a page brings nothing new (offset ignored);
        # without a total, a short page is the last one
        if not content or len(listed) == before or (total and offset >= total) or (not total and len(content) < PAGE):
            break

    if list_only:
        return await asyncio.to_thread(parse_oracle_hcm, listed, company_name, slug)

    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
    failed = 0

    async def detail(item: dict) -> dict:
        nonlocal failed
        async with sem:
            try:
                r = await _get(client, DETAIL_URL.format(host=host, site=site, id=item["Id"]))
                return {**item, "detail": _search(r.json())}
            except (httpx.HTTPError, ValueError, AttributeError):
                # keep the posting (so it isn't deactivated) with its short description
                failed += 1
                return item

    head, tail = listed[:MAX_DETAILS], listed[MAX_DETAILS:]
    details = list(await asyncio.gather(*(detail(i) for i in head)))
    if len(head) >= 3 and failed == len(head):  # one closed posting on a tiny board is not a broken board
        raise RuntimeError(f"all {failed} posting detail requests failed")
    return await asyncio.to_thread(parse_oracle_hcm, details + tail, company_name, slug)
