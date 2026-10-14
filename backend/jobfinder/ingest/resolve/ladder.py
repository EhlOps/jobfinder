"""Types shared by the resolver rungs.

A rung looks at a pasted URL (and, if it needs to, the page behind it) and returns one of:
  BoardHit   - a known ATS board (ats, slug) to add and fetch
  RecipeHit  - a validated recipe for a site that isn't on a known ATS (stored as ats='recipe')
  SingleJob  - just this one posting (e.g. from JSON-LD); kept, and the ladder carries on looking for the board
or None to pass. Rungs run cheapest first (RUNGS in rungs/__init__.py, sorted by `order`).
"""
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from jobfinder.ingest.base import JobPosting
from jobfinder.ingest.discovery.robots import PoliteFetcher
from jobfinder.ingest.http_safety import TEXT_TYPES, FetchError, SafeResponse, safe_fetch
from jobfinder.ingest.recipes import Recipe

PAGE_MAX_BYTES = 3_000_000


@dataclass
class BoardHit:
    ats: str
    slug: str
    name: str | None = None


@dataclass
class RecipeHit:
    recipe: Recipe
    domain: str  # registrable domain; becomes the board slug
    name: str | None = None
    postings: list[JobPosting] = field(default_factory=list)  # what validation already fetched


@dataclass
class SingleJob:
    posting: JobPosting


RungHit = BoardHit | RecipeHit | SingleJob


class ResolveContext:
    """What a rung gets: the pasted URL, an HTTP client, and the page behind the URL fetched at most once
    (SSRF-checked on every hop, size-capped, robots.txt honoured)."""

    def __init__(self, url: str, client: httpx.AsyncClient, *, allow_llm: bool = False,
                 robots: PoliteFetcher | None = None):
        self.url = url
        self.client = client
        self.allow_llm = allow_llm
        self.robots = robots if robots is not None else PoliteFetcher(client)
        self.notes: list[str] = []  # why rungs passed, for Admin > Coverage
        self.single_job: JobPosting | None = None
        self._page: SafeResponse | None = None
        self._page_error: str | None = None
        self._fetched = False

    def note(self, msg: str) -> None:
        self.notes.append(msg[:300])

    async def page(self) -> SafeResponse | None:
        """The pasted page, or None if it can't be fetched (the reason is noted once)."""
        if not self._fetched:
            self._fetched = True
            try:
                self._page = await safe_fetch(self.client, self.url, accept=TEXT_TYPES,
                                              max_bytes=PAGE_MAX_BYTES, robots=self.robots)
            except FetchError as e:
                self._page_error = str(e)
                self.note(f"page: {e}")
        return self._page

    @property
    def page_error(self) -> str | None:
        return self._page_error


class Rung(Protocol):
    name: str
    order: float

    async def run(self, ctx: ResolveContext) -> RungHit | None: ...
