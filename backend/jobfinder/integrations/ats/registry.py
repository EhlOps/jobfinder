"""The ATS registry: the name stored in companies.ats -> the fetcher that reads that kind of board."""
from collections.abc import Awaitable, Callable
from functools import partial

import httpx

from jobfinder.ingest.base import JobPosting
from jobfinder.integrations.ats.ashby import fetch_ashby
from jobfinder.integrations.ats.bamboohr import fetch_bamboohr
from jobfinder.integrations.ats.eightfold import fetch_eightfold
from jobfinder.integrations.ats.greenhouse import fetch_greenhouse
from jobfinder.integrations.ats.icims import fetch_icims
from jobfinder.integrations.ats.lever import fetch_lever
from jobfinder.integrations.ats.oracle_hcm import fetch_oracle_hcm
from jobfinder.integrations.ats.phenom import fetch_phenom
from jobfinder.integrations.ats.recruitee import fetch_recruitee
from jobfinder.integrations.ats.smartrecruiters import fetch_smartrecruiters
from jobfinder.integrations.ats.successfactors import fetch_successfactors
from jobfinder.integrations.ats.taleo import fetch_taleo
from jobfinder.integrations.ats.workable import fetch_workable
from jobfinder.integrations.ats.workday import fetch_workday

Fetcher = Callable[[httpx.AsyncClient, str, str], Awaitable[list[JobPosting]]]

# ATS name (as stored in companies.ats) -> fetcher(client, slug, company_name)
ATS_FETCHERS: dict[str, Fetcher] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workable": fetch_workable,
    "recruitee": fetch_recruitee,
    "bamboohr": fetch_bamboohr,
    "workday": fetch_workday,
    "phenom": fetch_phenom,
    "oracle_hcm": fetch_oracle_hcm,
    "successfactors": fetch_successfactors,
    "icims": fetch_icims,
    "eightfold": fetch_eightfold,
    "taleo": fetch_taleo,
}

# Cheap existence probes (no per-posting detail requests) for the ATSes whose full fetch fans out; others reuse the full fetcher.
PROBE_FETCHERS: dict[str, Fetcher] = {
    **ATS_FETCHERS,
    "smartrecruiters": partial(fetch_smartrecruiters, list_only=True),
    "bamboohr": partial(fetch_bamboohr, list_only=True),
    "workday": partial(fetch_workday, list_only=True),
    "phenom": partial(fetch_phenom, list_only=True),
    "oracle_hcm": partial(fetch_oracle_hcm, list_only=True),
    "successfactors": partial(fetch_successfactors, list_only=True),
    "icims": partial(fetch_icims, list_only=True),
    "eightfold": partial(fetch_eightfold, list_only=True),
    "taleo": partial(fetch_taleo, list_only=True),
}
