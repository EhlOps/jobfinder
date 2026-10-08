import asyncio
from datetime import UTC, datetime

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

API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
PAGE = 100
MAX_PAGES = 20  # 2000 postings per company is plenty
DETAIL_CONCURRENCY = 8


def _dt(s: str | None) -> datetime | None:
    try:
        d = datetime.fromisoformat(s) if s else None
    except ValueError:
        return None
    return d.replace(tzinfo=UTC) if d and d.tzinfo is None else d


def _description(j: dict) -> str:
    sections = ((j.get("jobAd") or {}).get("sections")) or {}
    # sections come back in display order; each is {title, text} with HTML text
    html = "\n".join(s.get("text") or "" for s in sections.values() if isinstance(s, dict))
    return html_to_text(html)


def parse_smartrecruiters(postings: list[dict], company_name: str) -> list[JobPosting]:
    """`postings` are posting-detail objects (the list endpoint has no description or apply URL)."""
    out = []
    for j in postings:
        title = (j.get("name") or "").strip()
        ext_id = j.get("id") or j.get("uuid")
        if not title or not ext_id:
            continue
        loc = j.get("location") or {}
        location = loc.get("fullLocation") or ", ".join(p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p)
        declared = "hybrid" if loc.get("hybrid") else "remote" if loc.get("remote") else None
        desc = _description(j)
        sal = parse_salary_range(desc)
        released = j.get("releasedDate")
        out.append(
            JobPosting(
                source="smartrecruiters",
                external_id=str(ext_id),
                company_name=company_name,
                title=title,
                url=safe_http_url(j.get("postingUrl") or j.get("applyUrl")),
                location=location,
                workplace_type=infer_workplace(location, title, declared=declared),
                employment_type=(j.get("typeOfEmployment") or {}).get("label"),
                description_text=clip(desc),
                salary_min=sal[0] if sal else None,
                salary_max=sal[1] if sal else None,
                salary_currency=sal[2] if sal else None,
                seniority=infer_seniority(title),
                posted_at=_dt(released),
            )
        )
    return out


async def fetch_smartrecruiters(client: httpx.AsyncClient, slug: str, company_name: str) -> list[JobPosting]:
    listed: list[dict] = []
    for page in range(MAX_PAGES):
        resp = await client.get(API.format(slug=slug), params={"limit": PAGE, "offset": page * PAGE})
        resp.raise_for_status()
        body = resp.json()
        content = body.get("content") or []
        listed.extend(c for c in content if c.get("id"))
        if not content or len(listed) >= body.get("totalFound", 0):
            break

    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    failed = 0

    async def detail(item: dict) -> dict:
        nonlocal failed
        async with sem:
            try:
                r = await client.get(f"{API.format(slug=slug)}/{item.get('id')}")
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError):
                # keep the posting (so it isn't deactivated) with an empty description; the upsert
                # preserves any stored description
                failed += 1
                return {**item, "postingUrl": f"https://jobs.smartrecruiters.com/{slug}/{item.get('id')}"}

    details = list(await asyncio.gather(*(detail(i) for i in listed)))
    if listed and failed == len(listed):
        raise RuntimeError(f"all {failed} posting detail requests failed")
    return parse_smartrecruiters(details, company_name)
