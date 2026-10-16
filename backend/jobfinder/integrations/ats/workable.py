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

API = "https://apply.workable.com/api/v1/widget/accounts/{slug}"


def _dt(s: str | None) -> datetime | None:
    try:
        d = datetime.fromisoformat(s) if s else None
    except ValueError:
        return None
    return d.replace(tzinfo=UTC) if d and d.tzinfo is None else d


def _join(*parts: str | None) -> str:
    return ", ".join(p for p in parts if p)


def _location(j: dict) -> str:
    locs = [_join(x.get("city"), x.get("region"), x.get("country")) for x in j.get("locations") or [] if not x.get("hidden")]
    if not j.get("locations"):
        locs = [_join(j.get("city"), j.get("state"), j.get("country"))]
    return "; ".join(dict.fromkeys(x for x in locs if x))


def parse_workable(payload: dict, company_name: str) -> list[JobPosting]:
    out = []
    for j in payload.get("jobs", []):
        title = (j.get("title") or "").strip()
        ext_id = j.get("shortcode") or j.get("code")
        if not title or not ext_id:
            continue
        location = _location(j)
        desc = html_to_text(j.get("description") or "")
        declared = "remote" if j.get("telecommuting") else None
        sal = parse_salary_range(desc)
        posted = j.get("published_on") or j.get("created_at")
        out.append(
            JobPosting(
                source="workable",
                external_id=str(ext_id),
                company_name=company_name,
                title=title,
                url=safe_http_url(j.get("url") or j.get("shortlink") or j.get("application_url")),
                location=location,
                workplace_type=infer_workplace(location, title, declared=declared),
                employment_type=j.get("employment_type"),
                description_text=clip(desc),
                salary_min=sal[0] if sal else None,
                salary_max=sal[1] if sal else None,
                salary_currency=sal[2] if sal else None,
                seniority=infer_seniority(title),
                posted_at=_dt(posted),
            )
        )
    return out


async def fetch_workable(client: httpx.AsyncClient, slug: str, company_name: str) -> list[JobPosting]:
    resp = await client.get(API.format(slug=slug), params={"details": "true"})
    resp.raise_for_status()
    return parse_workable(resp.json(), company_name)
