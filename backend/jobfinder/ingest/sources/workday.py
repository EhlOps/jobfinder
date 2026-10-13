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

_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_WD_RE = re.compile(r"^wd\d{1,3}$", re.IGNORECASE)
_SITE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_PATH_RE = re.compile(r"^/[^?#\s]+$")
_POSTED_RE = re.compile(r"posted\s+(\d+)\+?\s+days?\s+ago", re.IGNORECASE)


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
        ext_id = path.rstrip("/").rsplit("/", 1)[-1]
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
                posted_at=parse_posted_on(it.get("postedOn") or d.get("postedOn"), now),
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
            resp = await client.post(url, json=body)
            resp.raise_for_status()
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
                r = await client.get(DETAIL_API.format(tenant=tenant, wd=wd, site=site, path=item["externalPath"]))
                r.raise_for_status()
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
