"""New-grad job lists on GitHub: the raw README/markdown holds the apply links."""
from jobfinder.ingest.discovery import Source, boards_from_text
from jobfinder.ingest.discovery.robots import PoliteFetcher

URLS = ["https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md"]


async def fetch(fetcher: PoliteFetcher) -> list[tuple[str, str]]:
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for url in URLS:
        for b in boards_from_text((await fetcher.get(url)).text):
            out.setdefault((b[0], b[1].lower()), b)
    return list(out.values())


SOURCE = Source(
    "github_lists",
    "Public community-maintained GitHub lists read through raw.githubusercontent.com: one file per list, "
    "fetched at most weekly, subject to that host's robots.txt.",
    True,
    fetch,
)
