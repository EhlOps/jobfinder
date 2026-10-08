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
)

BASE = "https://{slug}.bamboohr.com/careers"
# The list endpoint has no descriptions; each opening needs one /detail call.
DETAIL_CONCURRENCY = 5
_LOCATION_TYPES = {"0": "onsite", "1": "remote", "2": "hybrid"}


def _location(j: dict) -> str:
    loc = j.get("location") or {}
    return ", ".join(x.strip() for x in (loc.get("city"), loc.get("state")) if x and x.strip())


def _posted(raw: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt and dt.tzinfo is None else dt


def parse_bamboohr(listing: dict, details: dict[str, dict], company_name: str, slug: str) -> list[JobPosting]:
    """`details` maps opening id -> /careers/<id>/detail payload; a missing one just means no description."""
    out = []
    for j in listing.get("result") or []:
        title = (j.get("jobOpeningName") or "").strip()
        if not title or not j.get("id"):
            continue
        jid = str(j["id"])
        d = ((details.get(jid) or {}).get("result") or {}).get("jobOpening") or {}
        location = _location(j) or _location(d)
        declared = _LOCATION_TYPES.get(str(j.get("locationType")))
        if declared is None and (j.get("isRemote") or d.get("isRemote")):
            declared = "remote"
        desc = html_to_text(d.get("description") or "")
        sal = parse_salary_range(desc)
        posted = d.get("datePosted")
        out.append(
            JobPosting(
                source="bamboohr",
                external_id=jid,
                company_name=company_name,
                title=title,
                url=f"{BASE.format(slug=slug)}/{jid}",
                location=location or ("Remote" if declared == "remote" else ""),
                workplace_type=infer_workplace(location, title, declared=declared),
                employment_type=j.get("employmentStatusLabel"),
                description_text=clip(desc),
                salary_min=sal[0] if sal else None,
                salary_max=sal[1] if sal else None,
                salary_currency=sal[2] if sal else None,
                seniority=infer_seniority(title),
                posted_at=_posted(posted),
            )
        )
    return out


async def fetch_bamboohr(client: httpx.AsyncClient, slug: str, company_name: str) -> list[JobPosting]:
    base = BASE.format(slug=slug)
    resp = await client.get(f"{base}/list", headers={"Accept": "application/json"})
    resp.raise_for_status()
    listing = resp.json()
    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    failed = 0

    async def detail(jid: str) -> tuple[str, dict]:
        nonlocal failed
        async with sem:
            try:
                r = await client.get(f"{base}/{jid}/detail", headers={"Accept": "application/json"})
                r.raise_for_status()
                return jid, r.json()
            except (httpx.HTTPError, ValueError):
                failed += 1
                return jid, {}  # keep the job (the upsert preserves any stored description)

    ids = [str(j["id"]) for j in listing.get("result") or [] if j.get("id")]
    details = dict(await asyncio.gather(*(detail(i) for i in ids)))
    if ids and failed == len(ids):
        raise RuntimeError(f"all {failed} opening detail requests failed")
    return parse_bamboohr(listing, details, company_name, slug)
