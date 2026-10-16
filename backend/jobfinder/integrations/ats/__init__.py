"""The ATS job-board fetchers: one module per vendor, each `fetch_<vendor>(client, slug, company_name)`.

The registry (ATS name -> fetcher) is ats/registry.py. This package file stays free of imports, so a
vendor module, or ingest/http_safety.py reading ats/custom, can import from it without a cycle.
"""
import re

# A board slug as stored in companies.slug (validate.SLUG_RE re-exports it).
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
