"""Polite fetching for discovery: a descriptive User-Agent, robots.txt checks and low volume."""
import logging
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger("jobfinder.discovery")
USER_AGENT = "JobFinderDiscovery/1.0 (+personal job search tool; polite, low volume)"
ROBOTS_TOKEN = "JobFinderDiscovery"


class RobotsDisallowed(Exception):
    """robots.txt (or an unreadable robots.txt) forbids fetching this URL."""


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT})


class PoliteFetcher:
    """Wraps a client: every GET first consults the host's robots.txt (cached per origin)."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._robots: dict[str, RobotFileParser | None] = {}

    async def _parser(self, origin: str) -> RobotFileParser | None:
        if origin in self._robots:
            return self._robots[origin]
        parser: RobotFileParser | None = RobotFileParser()
        try:
            r = await self.client.get(f"{origin}/robots.txt", headers={"User-Agent": USER_AGENT})
            if r.status_code in (401, 403):
                parser.disallow_all = True
            elif r.status_code in (404, 410):
                parser.allow_all = True  # no robots.txt: nothing is forbidden
            elif r.status_code >= 400:
                parser = None  # cannot tell: be conservative
            else:
                parser.parse(r.text.splitlines())
        except httpx.HTTPError:
            parser = None
        self._robots[origin] = parser
        return parser

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        parser = await self._parser(f"{parts.scheme}://{parts.netloc}")
        if parser is None:
            return False
        return parser.can_fetch(ROBOTS_TOKEN, url)

    async def get(self, url: str, **kwargs) -> httpx.Response:
        if not await self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        r = await self.client.get(url, headers={"User-Agent": USER_AGENT}, **kwargs)
        r.raise_for_status()
        return r
