import html
from datetime import datetime

import httpx

from jobfinder.ingest.base import (
    JobPosting,
    clip,
    html_to_text,
    infer_seniority,
    infer_workplace,
    parse_salary_range,
)

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


def parse_greenhouse(payload: dict, company_name: str) -> list[JobPosting]:
    out = []
    for j in payload.get("jobs", []):
        title = (j.get("title") or "").strip()
        if not title or not j.get("id"):
            continue
        location = ((j.get("location") or {}).get("name") or "").strip()
        # `content` is HTML that has been HTML-escaped a second time.
        desc = html_to_text(html.unescape(j.get("content") or ""))
        lo = hi = cur = None
        if ranges := j.get("pay_input_ranges"):
            r = ranges[0]
            if r.get("min_cents") is not None and r.get("max_cents") is not None:
                lo, hi, cur = r["min_cents"] // 100, r["max_cents"] // 100, r.get("currency_type") or "USD"
        if lo is None and (found := parse_salary_range(desc)):
            lo, hi, cur = found
        posted = j.get("first_published") or j.get("updated_at")
        out.append(
            JobPosting(
                source="greenhouse",
                external_id=str(j["id"]),
                company_name=company_name,
                title=title,
                url=j.get("absolute_url") or "",
                location=location,
                workplace_type=infer_workplace(location, title),
                description_text=clip(desc),
                salary_min=lo,
                salary_max=hi,
                salary_currency=cur,
                seniority=infer_seniority(title),
                posted_at=datetime.fromisoformat(posted) if posted else None,
            )
        )
    return out


async def fetch_greenhouse(client: httpx.AsyncClient, slug: str, company_name: str) -> list[JobPosting]:
    resp = await client.get(API.format(slug=slug), params={"content": "true"})
    resp.raise_for_status()
    return parse_greenhouse(resp.json(), company_name)
