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

API = "https://{slug}.recruitee.com/api/offers/"


def _posted(o: dict) -> datetime | None:
    # e.g. "2026-10-05 16:05:18 UTC"
    raw = (o.get("published_at") or o.get("created_at") or "").removesuffix(" UTC").strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _salary(o: dict) -> tuple[int, int, str] | None:
    s = o.get("salary") or {}
    if str(s.get("period") or "").lower() != "year":
        return None
    try:
        lo, hi = int(float(s["min"])), int(float(s["max"]))
    except (KeyError, TypeError, ValueError):
        return None
    if lo <= 0 or hi < lo:
        return None
    return lo, hi, s.get("currency") or "USD"


def parse_recruitee(payload: dict, company_name: str) -> list[JobPosting]:
    out = []
    for o in payload.get("offers", []):
        title = (o.get("title") or "").strip()
        if not title or not o.get("id") or o.get("status", "published") != "published":
            continue
        location = (o.get("location") or "").strip()
        desc = html_to_text(f"{o.get('description') or ''}\n{o.get('requirements') or ''}")
        # Recruitee has explicit flags rather than a single workplace field.
        declared = "remote" if o.get("remote") else "hybrid" if o.get("hybrid") else "onsite" if o.get("on_site") else None
        sal = _salary(o) or parse_salary_range(desc)
        out.append(
            JobPosting(
                source="recruitee",
                external_id=str(o["id"]),
                company_name=company_name,
                title=title,
                url=safe_http_url(o.get("careers_url") or o.get("careers_apply_url")),
                location=location,
                workplace_type=infer_workplace(location, title, declared=declared),
                employment_type=o.get("employment_type_code"),
                description_text=clip(desc),
                salary_min=sal[0] if sal else None,
                salary_max=sal[1] if sal else None,
                salary_currency=sal[2] if sal else None,
                seniority=infer_seniority(title),
                posted_at=_posted(o),
            )
        )
    return out


async def fetch_recruitee(client: httpx.AsyncClient, slug: str, company_name: str) -> list[JobPosting]:
    resp = await client.get(API.format(slug=slug))
    resp.raise_for_status()
    return parse_recruitee(resp.json(), company_name)
