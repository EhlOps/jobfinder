"""US federal jobs from the official USAJobs Search API. Opt-in: enabled when USAJOBS_API_KEY is set
(free key: https://developer.usajobs.gov/APIRequest). Isolated like jobspy: any failure here must never
affect the ATS ingestion.

Queries cover the engineering/IT/program-management occupational series (SERIES), nationwide plus one
query per target location of the users that is a real place."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.config import get_settings
from jobfinder.ingest.base import (
    MAX_DESCRIPTION_CHARS,
    JobPosting,
    clip,
    html_to_text,
    infer_seniority,
    infer_workplace,
    safe_http_url,
)
from jobfinder.matching.prefilter import refuses_sponsorship
from jobfinder.models import Profile, User

log = logging.getLogger("jobfinder.ingest.usajobs")
SOURCE = "usajobs"
API_URL = "https://data.usajobs.gov/api/search"
HOST = "data.usajobs.gov"
PAUSE_BETWEEN_REQUESTS_S = 1.0  # polite to the API
RESULTS_PER_PAGE = 500  # API maximum
CITIZENSHIP_NOTE = "U.S. citizenship is required. No visa sponsorship is available for this federal position."
# 0801 general engineering, 0830 mechanical, 0850 electrical, 0855 electronics, 0861 aerospace,
# 0893 chemical, 1550 computer science, 2210 IT management, 0340 program management,
# 0343 management/program analysis, 1515 operations research
SERIES = ("0801", "0830", "0850", "0855", "0861", "0893", "1550", "2210", "0340", "0343", "1515")
_NOT_PLACES = {"remote", "anywhere", "united states", "us", "usa", "nationwide", "hybrid", "onsite", "on-site"}
_ANNUAL = {"PA": 1, "PH": 2080, "PW": 52, "BW": 26, "SM": 24, "PM": 12, "PD": 260}  # RateIntervalCode


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _salary(descriptor: dict) -> tuple[int, int] | None:
    for r in descriptor.get("PositionRemuneration") or []:
        lo, hi = _num(r.get("MinimumRange")), _num(r.get("MaximumRange"))
        factor = _ANNUAL.get(str(r.get("RateIntervalCode") or "PA").upper())
        if lo and factor and lo > 0:
            return int(lo * factor), int((hi or lo) * factor)
    return None


def _date(s) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _text(v) -> str:
    if isinstance(v, list):
        v = "\n".join(str(x) for x in v if x)
    return html_to_text(str(v)) if v else ""


def parse_usajobs(data: dict) -> list[JobPosting]:
    out = []
    for item in ((data.get("SearchResult") or {}).get("SearchResultItems")) or []:
        d = item.get("MatchedObjectDescriptor") or {}
        ext, title = str(item.get("MatchedObjectId") or ""), (d.get("PositionTitle") or "").strip()
        applies = d.get("ApplyURI") or []
        url = safe_http_url(d.get("PositionURI")) or safe_http_url(applies[0] if applies else "")
        if not ext or not title or not url:
            continue
        details = (d.get("UserArea") or {}).get("Details") or {}
        parts = [_text(details.get("JobSummary")), _text(details.get("MajorDuties")), _text(details.get("Requirements"))]
        desc = "\n\n".join(p for p in parts if p)[: MAX_DESCRIPTION_CHARS - len(CITIZENSHIP_NOTE) - 2]
        if not refuses_sponsorship(desc):  # the note is what the sponsorship filter keys on
            desc = f"{desc}\n\n{CITIZENSHIP_NOTE}".strip()
        names = [str(x["LocationName"]) for x in d.get("PositionLocation") or [] if x.get("LocationName")]
        location = "; ".join(dict.fromkeys(names))
        remote = "remote" if str(details.get("RemoteIndicator")).lower() == "true" else None
        pay = _salary(d)
        sched = ((d.get("PositionSchedule") or [{}])[0]).get("Name")
        out.append(
            JobPosting(
                source=SOURCE, external_id=ext,
                company_name=d.get("OrganizationName") or d.get("DepartmentName") or "US Federal Government",
                title=title, url=url, location=location,
                workplace_type=infer_workplace(location, title, declared=remote),
                employment_type=sched, description_text=clip(desc),
                salary_min=pay[0] if pay else None, salary_max=pay[1] if pay else None,
                salary_currency="USD" if pay else None, seniority=infer_seniority(title),
                posted_at=_date(d.get("PublicationStartDate")),
            )
        )
    return out


async def build_locations(db: AsyncSession, limit: int | None = None) -> list[str | None]:
    """Nationwide (None) plus distinct place names from users' target locations, capped."""
    limit = limit or get_settings().usajobs_max_locations
    seen: dict[str, str] = {}
    q = sa.select(Profile.status).join(User, User.id == Profile.user_id).where(User.activated_at.is_not(None))
    for (status,) in (await db.execute(q.order_by(Profile.user_id))).all():
        for loc in (status or {}).get("target_locations") or []:
            loc = str(loc).strip()
            if loc and loc.lower() not in _NOT_PLACES:
                seen.setdefault(loc.lower(), loc)
    return [None, *list(seen.values())[:limit]]


async def _search(
    client: httpx.AsyncClient, headers: dict, location: str | None, max_pages: int, days: int
) -> list[JobPosting]:
    out: list[JobPosting] = []
    for page in range(1, max_pages + 1):
        if page > 1:
            await asyncio.sleep(PAUSE_BETWEEN_REQUESTS_S)
        params = {"JobCategoryCode": ";".join(SERIES), "ResultsPerPage": RESULTS_PER_PAGE, "Page": page,
                  "DatePosted": days}
        if location:
            params["LocationName"] = location
        for attempt in range(3):  # 429/5xx: back off and retry, then give up on this query
            r = await client.get(API_URL, params=params, headers=headers)
            if r.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                break
            await asyncio.sleep(PAUSE_BETWEEN_REQUESTS_S * 5 * (attempt + 1))
        r.raise_for_status()
        data = r.json()
        out += parse_usajobs(data)
        result = data.get("SearchResult") or {}
        total = int(result.get("SearchResultCountAll") or 0)
        if page * RESULTS_PER_PAGE >= total or not result.get("SearchResultItems"):
            break
        if page == max_pages:
            log.warning("usajobs query %s truncated at %d of %d results", location or "nationwide", len(out), total)
    return out


async def fetch_usajobs(client: httpx.AsyncClient, locations: list[str | None] | None = None) -> list[JobPosting]:
    """No-op without an API key. One failing location never loses the others; if every query fails, the first error is raised."""
    s = get_settings()
    if not s.usajobs_api_key:
        return []
    headers = {"Host": HOST, "User-Agent": s.usajobs_email or "jobfinder", "Authorization-Key": s.usajobs_api_key}
    postings: list[JobPosting] = []
    errors: list[Exception] = []
    for i, loc in enumerate(locations or [None]):
        if i:
            await asyncio.sleep(PAUSE_BETWEEN_REQUESTS_S)
        try:
            postings += await _search(client, headers, loc, s.usajobs_max_pages, s.usajobs_days_posted)
        except Exception as e:
            errors.append(e)
            log.warning("usajobs query %s failed: %s", loc or "nationwide", e)
    if errors and len(errors) == len(locations or [None]):
        raise errors[0]  # e.g. a bad API key: report it instead of looking like an empty result
    return postings
