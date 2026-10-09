from collections.abc import Awaitable, Callable
from functools import partial

import httpx

from jobfinder.ingest.base import JobPosting
from jobfinder.ingest.sources.ashby import fetch_ashby
from jobfinder.ingest.sources.bamboohr import fetch_bamboohr
from jobfinder.ingest.sources.greenhouse import fetch_greenhouse
from jobfinder.ingest.sources.lever import fetch_lever
from jobfinder.ingest.sources.recruitee import fetch_recruitee
from jobfinder.ingest.sources.smartrecruiters import fetch_smartrecruiters
from jobfinder.ingest.sources.workable import fetch_workable

Fetcher = Callable[[httpx.AsyncClient, str, str], Awaitable[list[JobPosting]]]

# ATS name (as used in companies.yaml / companies.ats) -> fetcher(client, slug, company_name)
ATS_FETCHERS: dict[str, Fetcher] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workable": fetch_workable,
    "recruitee": fetch_recruitee,
    "bamboohr": fetch_bamboohr,
}

# Cheap existence probes (no per-posting detail requests) for the ATSes whose full fetch fans out; others reuse the full fetcher.
PROBE_FETCHERS: dict[str, Fetcher] = {
    **ATS_FETCHERS,
    "smartrecruiters": partial(fetch_smartrecruiters, list_only=True),
    "bamboohr": partial(fetch_bamboohr, list_only=True),
}
