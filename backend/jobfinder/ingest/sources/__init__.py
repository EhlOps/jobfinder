from collections.abc import Awaitable, Callable

import httpx

from jobfinder.ingest.base import JobPosting
from jobfinder.ingest.sources.ashby import fetch_ashby
from jobfinder.ingest.sources.bamboohr import fetch_bamboohr
from jobfinder.ingest.sources.greenhouse import fetch_greenhouse
from jobfinder.ingest.sources.lever import fetch_lever
from jobfinder.ingest.sources.recruitee import fetch_recruitee

Fetcher = Callable[[httpx.AsyncClient, str, str], Awaitable[list[JobPosting]]]

# ATS name (as used in companies.yaml / companies.ats) -> fetcher(client, slug, company_name)
ATS_FETCHERS: dict[str, Fetcher] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "recruitee": fetch_recruitee,
    "bamboohr": fetch_bamboohr,
}
