import asyncio
import re
from datetime import UTC, datetime, timedelta

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

# Slug format: "{tenant}.{wdN}.{site}", e.g. "boeing.wd1.EXTERNAL_CAREERS".
BASE = "https://{tenant}.{wd}.myworkdayjobs.com"
LIST_API = BASE + "/wday/cxs/{tenant}/{site}/jobs"
DETAIL_API = BASE + "/wday/cxs/{tenant}/{site}{path}"
APPLY_URL = BASE + "/{site}{path}"
PAGE = 20  # Workday rejects larger page sizes
MAX_PAGES = 100  # 2000 listed postings; a 400/404/422 past page 0 ends paging early
MAX_DETAILS = 300  # per-board cap on description fetches; the rest keep title/location only
DETAIL_CONCURRENCY = 8
HOST_CONCURRENCY = 4  # boards share wdN hosts; cap in-flight requests per host across all boards
MAX_ATTEMPTS = 3
MAX_RETRY_WAIT = 30.0
_host_sems: dict[tuple[int, str], asyncio.Semaphore] = {}

_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_WD_RE = re.compile(r"^wd\d{1,3}$", re.IGNORECASE)
_SITE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_PATH_RE = re.compile(r"^/[^?#\s]+$")
_POSTED_RE = re.compile(r"posted\s+(\d+)\+?\s+days?\s+ago", re.IGNORECASE)


def _host_sem(host: str) -> asyncio.Semaphore:
    key = (id(asyncio.get_running_loop()), host)
    if key not in _host_sems:
        _host_sems[key] = asyncio.Semaphore(HOST_CONCURRENCY)
    return _host_sems[key]


def _retry_wait(resp: httpx.Response, attempt: int) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", "")), MAX_RETRY_WAIT)
    except ValueError:
        return min(2.0 ** attempt, MAX_RETRY_WAIT)


async def _request(client: httpx.AsyncClient, method: str, url: str, **kw) -> httpx.Response:
    """Per-host throttled request that backs off on 429/503 (Workday rate-limits shared wdN hosts)."""
    host = httpx.URL(url).host
    for attempt in range(MAX_ATTEMPTS):
        async with _host_sem(host):
            resp = await client.request(method, url, **kw)
        if resp.status_code not in (429, 503) or attempt == MAX_ATTEMPTS - 1:
            resp.raise_for_status()
            return resp
        await asyncio.sleep(_retry_wait(resp, attempt))
    raise AssertionError("unreachable")


def parse_slug(slug: str) -> tuple[str, str, str]:
    """(tenant, wdN, site) from "{tenant}.{wdN}.{site}"; ValueError when malformed."""
    parts = (slug or "").split(".", 2)
    if len(parts) != 3 or not (_TENANT_RE.match(parts[0]) and _WD_RE.match(parts[1]) and _SITE_RE.match(parts[2])):
        raise ValueError(f"invalid workday slug '{slug}' (expected tenant.wdN.site)")
    return parts[0], parts[1].lower(), parts[2]


def parse_posted_on(text: str | None, now: datetime | None = None) -> datetime | None:
    """"Posted Today" / "Posted Yesterday" / "Posted 5 Days Ago" / "Posted 30+ Days Ago" -> midnight UTC."""
    if not text:
        return None
    today = (now or datetime.now(UTC)).replace(hour=0, minute=0, second=0, microsecond=0)
    t = text.strip().lower()
    if "today" in t:
        return today
    if "yesterday" in t:
        return today - timedelta(days=1)
    m = _POSTED_RE.search(t)
    if not m or "+" in m.group(0):  # "30+ Days Ago" is only a lower bound; don't invent a drifting date
        return None
    return today - timedelta(days=int(m.group(1)))


def _start_date(detail: dict) -> datetime | None:
    """The detail's `startDate` ("2026-10-12") is exact, unlike the listing's "Posted 30+ Days Ago"."""
    try:
        return datetime.strptime(str(detail.get("startDate") or "")[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _declared(remote_type: str | None) -> str | None:
    """"Hybrid Job" / "Remote Job" / "Onsite" -> the bare word infer_workplace understands."""
    return re.sub(r"\s*(job|work)s?$", "", remote_type.strip(), flags=re.IGNORECASE) if remote_type else None


def parse_workday(items: list[dict], company_name: str, slug: str, now: datetime | None = None) -> list[JobPosting]:
    """`items` are listing entries; a "detail" key may hold the posting's `jobPostingInfo` object."""
    tenant, wd, site = parse_slug(slug)
    out = []
    for it in items:
        d = it.get("detail") or {}
        title = (it.get("title") or d.get("title") or "").strip()
        path = it.get("externalPath") or ""
        if not title or not _PATH_RE.match(path) or ".." in path:
            continue
        last = path.rstrip("/").rsplit("/", 1)[-1]
        ext_id = last.rsplit("_", 1)[-1] if "_" in last else last  # req id only; the title part of the path can be edited
        location = (d.get("location") or it.get("locationsText") or "").strip()
        desc = html_to_text(d.get("jobDescription") or "")
        sal = parse_salary_range(desc)
        url = safe_http_url(d.get("externalUrl")) or APPLY_URL.format(tenant=tenant, wd=wd, site=site, path=path)
        out.append(
            JobPosting(
                source="workday",
                external_id=f"{tenant}:{ext_id}",  # unique across tenants (Job key is source+external_id)
                company_name=company_name,
                title=title,
                url=url,
                location=location,
                workplace_type=infer_workplace(location, title, declared=_declared(d.get("remoteType") or it.get("remoteType"))),
                employment_type=d.get("timeType") or None,
                description_text=clip(desc),
                salary_min=sal[0] if sal else None,
                salary_max=sal[1] if sal else None,
                salary_currency=sal[2] if sal else None,
                seniority=infer_seniority(title),
                posted_at=parse_posted_on(it.get("postedOn") or d.get("postedOn"), now) or _start_date(d),
            )
        )
    return out


async def fetch_workday(
    client: httpx.AsyncClient, slug: str, company_name: str, list_only: bool = False
) -> list[JobPosting]:
    """list_only fetches just the first listing page and skips the per-posting detail calls (cheap probe)."""
    tenant, wd, site = parse_slug(slug)
    url = LIST_API.format(tenant=tenant, wd=wd, site=site)
    listed: list[dict] = []
    total = 0
    for page in range(1 if list_only else MAX_PAGES):
        body = {"appliedFacets": {}, "limit": PAGE, "offset": page * PAGE, "searchText": ""}
        try:
            resp = await _request(client, "POST", url, json=body)
        except httpx.HTTPStatusError as e:
            # Workday refuses offsets past its cap: keep what we have, but a failing first page is a real error
            if page > 0 and e.response.status_code in (400, 404, 422):
                break
            raise
        data = resp.json()
        content = data.get("jobPostings") or []
        listed.extend(c for c in content if _PATH_RE.match(c.get("externalPath") or ""))
        total = total or data.get("total") or 0  # only the first page reports a total
        if not content or (total and len(listed) >= total):
            break

    if list_only:
        return parse_workday(listed, company_name, slug)

    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
    failed = 0

    async def detail(item: dict) -> dict:
        nonlocal failed
        async with sem:
            try:
                r = await _request(client, "GET", DETAIL_API.format(tenant=tenant, wd=wd, site=site, path=item["externalPath"]))
                return {**item, "detail": r.json().get("jobPostingInfo") or {}}
            except (httpx.HTTPError, ValueError, AttributeError):
                # keep the posting (so it isn't deactivated) with an empty description; the upsert
                # preserves any stored description
                failed += 1
                return item

    head, tail = listed[:MAX_DETAILS], listed[MAX_DETAILS:]
    details = list(await asyncio.gather(*(detail(i) for i in head)))
    if len(head) >= 3 and failed == len(head):
        raise RuntimeError(f"all {failed} posting detail requests failed")
    return parse_workday(details + tail, company_name, slug)
