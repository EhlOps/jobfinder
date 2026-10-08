"""Sources recorded as skipped: no terms grant automated access, so nothing is requested."""
from jobfinder.ingest.discovery import Source

SOURCE = Source(
    "yc_work_at_a_startup",
    "Work at a Startup (workatastartup.com) is a login-gated, JavaScript-rendered application; "
    "no terms granting automated access were found.",
    False,
    skip_reason="Work at a Startup is login-gated and no terms permit automated access; not fetched",
)

WELLFOUND = Source(
    "wellfound",
    "Wellfound's terms and robots.txt forbid automated access and scraping.",
    False,
    skip_reason="Wellfound's terms and robots.txt forbid automated access; not fetched",
)
