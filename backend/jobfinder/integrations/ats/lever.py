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

API = "https://api.lever.co/v0/postings/{slug}"


def parse_lever(payload: list[dict], company_name: str) -> list[JobPosting]:
    out = []
    for j in payload:
        title = (j.get("text") or "").strip()
        if not title or not j.get("id"):
            continue
        cats = j.get("categories") or {}
        locations = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        location = "; ".join(locations)
        parts = [j.get("descriptionPlain") or j.get("openingPlain") or ""]
        for lst in j.get("lists") or []:  # requirement/benefit sections arrive as HTML lists
            parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content') or '')}")
        parts.append(j.get("additionalPlain") or "")
        desc = "\n\n".join(p.strip() for p in parts if p and p.strip())

        lo = hi = cur = None
        if (sr := j.get("salaryRange")) and str(sr.get("interval", "")).endswith("year-salary"):
            lo, hi, cur = sr.get("min"), sr.get("max"), sr.get("currency") or "USD"
        if lo is None and (found := parse_salary_range(desc)):
            lo, hi, cur = found
        created = j.get("createdAt")
        out.append(
            JobPosting(
                source="lever",
                external_id=str(j["id"]),
                company_name=company_name,
                title=title,
                url=j.get("hostedUrl") or "",
                location=location,
                workplace_type=infer_workplace(location, title, declared=j.get("workplaceType")),
                employment_type=cats.get("commitment"),
                description_text=clip(desc),
                salary_min=lo,
                salary_max=hi,
                salary_currency=cur,
                seniority=infer_seniority(title),
                posted_at=datetime.fromtimestamp(created / 1000, UTC) if created else None,
            )
        )
    return out


async def fetch_lever(client: httpx.AsyncClient, slug: str, company_name: str) -> list[JobPosting]:
    resp = await client.get(API.format(slug=slug), params={"mode": "json"})
    resp.raise_for_status()
    return parse_lever(resp.json(), company_name)
