"""Hacker News 'Ask HN: Who is hiring?' via the public Algolia API."""
import html

from jobfinder.ingest.discovery import Source, boards_from_text
from jobfinder.ingest.discovery.robots import PoliteFetcher

API = "https://hn.algolia.com/api/v1"
TITLE = "Ask HN: Who is hiring?"


async def fetch(fetcher: PoliteFetcher) -> list[tuple[str, str]]:
    r = await fetcher.get(
        f"{API}/search_by_date", params={"query": TITLE, "tags": "story,author_whoishiring", "hitsPerPage": 5}
    )
    hits = [h for h in r.json().get("hits", []) if str(h.get("title", "")).startswith(TITLE)]
    if not hits:
        return []
    story = (await fetcher.get(f"{API}/items/{hits[0]['objectID']}")).json()
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for comment in story.get("children") or []:  # top-level comments are the job posts
        for b in boards_from_text(html.unescape(comment.get("text") or "")):
            out.setdefault((b[0], b[1].lower()), b)
    return list(out.values())


SOURCE = Source(
    "hn_hiring",
    "Algolia's public Hacker News API (hn.algolia.com/api/v1): two read-only requests per run.",
    True,
    fetch,
)
