from datetime import datetime

import httpx

from jobfinder.ingest.base import (
    JobPosting,
    clip,
    infer_seniority,
    infer_workplace,
    parse_salary_range,
)

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def _salary(comp: dict | None) -> tuple[int, int, str] | None:
    for c in (comp or {}).get("summaryComponents") or []:
        is_annual_salary = c.get("compensationType") == "Salary" and "YEAR" in str(c.get("interval", "")).upper()
        if is_annual_salary and c.get("minValue") is not None and c.get("maxValue") is not None:
            return int(c["minValue"]), int(c["maxValue"]), c.get("currencyCode") or "USD"
    return None


def parse_ashby(payload: dict, company_name: str) -> list[JobPosting]:
    out = []
    for j in payload.get("jobs", []):
        title = (j.get("title") or "").strip()
        if not title or not j.get("id") or j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        location = "; ".join(dict.fromkeys(x for x in locs if x))
        desc = j.get("descriptionPlain") or ""
        declared = j.get("workplaceType") or ("remote" if j.get("isRemote") else None)
        sal = _salary(j.get("compensation")) or parse_salary_range(desc)
        published = j.get("publishedAt")
        out.append(
            JobPosting(
                source="ashby",
                external_id=str(j["id"]),
                company_name=company_name,
                title=title,
                url=j.get("jobUrl") or "",
                location=location,
                workplace_type=infer_workplace(location, title, declared=declared),
                employment_type=j.get("employmentType"),
                description_text=clip(desc),
                salary_min=sal[0] if sal else None,
                salary_max=sal[1] if sal else None,
                salary_currency=sal[2] if sal else None,
                seniority=infer_seniority(title),
                posted_at=datetime.fromisoformat(published) if published else None,
            )
        )
    return out


async def fetch_ashby(client: httpx.AsyncClient, slug: str, company_name: str) -> list[JobPosting]:
    resp = await client.get(API.format(slug=slug), params={"includeCompensation": "true"})
    resp.raise_for_status()
    return parse_ashby(resp.json(), company_name)
