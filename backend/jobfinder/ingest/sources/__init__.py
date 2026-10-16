"""Moved (#42): the ATS fetchers are jobfinder.integrations.ats (registry in .registry), and USAJOBS and
JobSpy are jobfinder.integrations.usajobs and .jobspy. These old names stay importable until #53, as the
same module objects (see ingest/_moved.py)."""
import pkgutil

from jobfinder.ingest._moved import alias
from jobfinder.integrations import ats

# The old __init__ was the registry, so it exported every fetcher it registered too.
from jobfinder.integrations.ats.registry import (  # noqa: F401
    ATS_FETCHERS,
    PROBE_FETCHERS,
    Fetcher,
    fetch_ashby,
    fetch_bamboohr,
    fetch_eightfold,
    fetch_greenhouse,
    fetch_icims,
    fetch_lever,
    fetch_oracle_hcm,
    fetch_phenom,
    fetch_recruitee,
    fetch_smartrecruiters,
    fetch_successfactors,
    fetch_taleo,
    fetch_workable,
    fetch_workday,
)

for _info in pkgutil.iter_modules(ats.__path__):
    alias(f"{__name__}.{_info.name}", f"{ats.__name__}.{_info.name}")
alias(f"{__name__}.usajobs", "jobfinder.integrations.usajobs")
alias(f"{__name__}.jobspy_source", "jobfinder.integrations.jobspy")
