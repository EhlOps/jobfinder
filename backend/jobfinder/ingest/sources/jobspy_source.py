"""LinkedIn/Indeed via python-jobspy. Opt-in (ENABLE_JOBSPY=true) and isolated: scraping those sites
violates their terms and is fragile, so any failure here must never affect the ATS ingestion.

python-jobspy is an optional dependency (`uv sync --extra jobspy`) and is imported lazily."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.config import get_settings
from jobfinder.ingest.base import (
    JobPosting,
    clip,
    infer_seniority,
    infer_workplace,
    parse_salary_range,
)
from jobfinder.models import Profile

log = logging.getLogger("jobfinder.ingest.jobspy")
SOURCE = "jobspy"
PAUSE_BETWEEN_QUERIES_S = 5  # be gentle: these sites rate-limit aggressively


@dataclass(frozen=True)
class JobQuery:
    role: str
    location: str


def _none(v):
    """pandas gives NaN/NaT/None for missing values."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return None if str(v) in ("NaT", "nan", "None", "") else v


def _annual(amount, interval) -> int | None:
    if amount is None:
        return None
    factor = {"yearly": 1, "monthly": 12, "weekly": 52, "daily": 260, "hourly": 2080}.get(str(interval).lower(), 1)
    return int(float(amount) * factor)


def parse_jobspy_rows(rows: list[dict]) -> list[JobPosting]:
    out = []
    for r in rows:
        r = {k: _none(v) for k, v in r.items()}
        title, url = (r.get("title") or "").strip(), r.get("job_url") or ""
        if not title or not url:
            continue
        ext = str(r.get("id") or hashlib.sha1(url.encode()).hexdigest()[:16])
        desc = clip(r.get("description") or "")
        lo, hi = _annual(r.get("min_amount"), r.get("interval")), _annual(r.get("max_amount"), r.get("interval"))
        cur = r.get("currency") or "USD"
        if lo is None and (found := parse_salary_range(desc)):
            lo, hi, cur = found
        posted = r.get("date_posted")
        if isinstance(posted, date) and not isinstance(posted, datetime):
            posted = datetime(posted.year, posted.month, posted.day, tzinfo=UTC)
        location = str(r.get("location") or "")
        out.append(
            JobPosting(
                source=SOURCE, external_id=ext, company_name=str(r.get("company") or "Unknown"),
                title=title, url=url, location=location,
                workplace_type=infer_workplace(location, title, declared="remote" if r.get("is_remote") is True else None),
                employment_type=str(r["job_type"]) if r.get("job_type") else None,
                description_text=desc, salary_min=lo, salary_max=hi, salary_currency=cur if lo else None,
                seniority=infer_seniority(title), posted_at=posted if isinstance(posted, datetime) else None,
            )
        )
    return out


async def build_queries(db: AsyncSession, limit: int | None = None) -> list[JobQuery]:
    """Distinct (role, location) pairs from all users' targets, capped."""
    limit = limit or get_settings().jobspy_max_queries
    seen: dict[JobQuery, None] = {}
    for (status,) in (await db.execute(sa.select(Profile.status))).all():
        roles = (status or {}).get("target_roles") or []
        locations = (status or {}).get("target_locations") or ["United States"]
        for role in roles:
            for loc in locations:
                seen[JobQuery(role.strip(), loc.strip())] = None
    return list(seen)[:limit]


def _scrape(query: JobQuery, results: int) -> list[dict]:
    try:
        from jobspy import scrape_jobs
    except ImportError:
        raise RuntimeError("python-jobspy is not installed (uv sync --extra jobspy)") from None
    df = scrape_jobs(
        site_name=["linkedin", "indeed"], search_term=query.role, location=query.location,
        results_wanted=results, hours_old=72 * 2, country_indeed="USA", linkedin_fetch_description=True,
        verbose=0,
    )
    return df.to_dict("records")


async def fetch_jobspy(queries: list[JobQuery], results_per_query: int | None = None) -> list[JobPosting]:
    n = results_per_query or get_settings().jobspy_results_per_query
    postings: list[JobPosting] = []
    for i, q in enumerate(queries):
        if i:
            await asyncio.sleep(PAUSE_BETWEEN_QUERIES_S)
        try:
            rows = await asyncio.to_thread(_scrape, q, n)
            postings += parse_jobspy_rows(rows)
        except Exception as e:  # one blocked query must not lose the others
            log.warning("jobspy query %s failed: %s", q, e)
    return postings
