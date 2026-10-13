"""Job lists on GitHub: the raw README/markdown/JSON holds the apply links, mapped to (ats, slug) boards."""
import logging

import httpx

from jobfinder.ingest.discovery import Source, boards_from_text
from jobfinder.ingest.discovery.robots import PoliteFetcher, RobotsDisallowed

log = logging.getLogger("jobfinder.discovery")

_RAW = "https://raw.githubusercontent.com"
_SIMPLIFY_LISTINGS = ".github/scripts/listings.json"

# Every URL was checked live and yields boards. The SimplifyJobs listings.json files carry every listing with a
# category (Software, Hardware = mechanical/electrical/aerospace/embedded, Quant, Product, AI/ML/Data), so they
# are the broadest sources; the READMEs add the other community lists.
URLS = [
    f"{_RAW}/SimplifyJobs/New-Grad-Positions/dev/README.md",
    f"{_RAW}/SimplifyJobs/New-Grad-Positions/dev/{_SIMPLIFY_LISTINGS}",
    f"{_RAW}/SimplifyJobs/Summer2026-Internships/dev/README.md",
    f"{_RAW}/SimplifyJobs/Summer2026-Internships/dev/README-Off-Season.md",
    f"{_RAW}/SimplifyJobs/Summer2027-Internships/dev/{_SIMPLIFY_LISTINGS}",
    f"{_RAW}/vanshb03/Summer2026-Internships/dev/README.md",
    f"{_RAW}/vanshb03/New-Grad-2026/dev/README.md",
    f"{_RAW}/speedyapply/2026-SWE-College-Jobs/main/README.md",
    f"{_RAW}/speedyapply/2026-SWE-College-Jobs/main/NEW_GRAD_USA.md",
    f"{_RAW}/speedyapply/2026-SWE-College-Jobs/main/NEW_GRAD_INTL.md",
    f"{_RAW}/speedyapply/2026-SWE-College-Jobs/main/INTERN_INTL.md",
    f"{_RAW}/speedyapply/2026-AI-College-Jobs/main/README.md",
    f"{_RAW}/speedyapply/2026-AI-College-Jobs/main/NEW_GRAD_USA.md",
]


async def fetch(fetcher: PoliteFetcher) -> list[tuple[str, str]]:
    """Boards from every list; one dead or blocked list is skipped, but all failing re-raises the last error."""
    out: dict[tuple[str, str], tuple[str, str]] = {}
    last: Exception | None = None
    ok = 0
    for url in URLS:
        try:
            text = (await fetcher.get(url)).text
        except (httpx.HTTPError, RobotsDisallowed) as e:
            log.warning("github list %s unavailable: %s", url, e)
            last = e
            continue
        ok += 1
        for b in boards_from_text(text):
            out.setdefault((b[0], b[1].lower()), b)
    if not ok and last is not None:
        raise last
    return list(out.values())


SOURCE = Source(
    "github_lists",
    "Public community-maintained GitHub lists read through raw.githubusercontent.com: a handful of README and "
    "JSON files, each fetched at most weekly, subject to that host's robots.txt.",
    True,
    fetch,
)
