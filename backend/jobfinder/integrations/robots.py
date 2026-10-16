"""Polite fetching for discovery and the ATS fetchers: a descriptive User-Agent, robots.txt checks and low
volume."""
import asyncio
import logging
from urllib.parse import quote, unquote, urlparse, urlsplit, urlunparse
from urllib.robotparser import RobotFileParser

import httpx

from jobfinder.ingest.http_safety import TEXT_TYPES, FetchError, safe_fetch

log = logging.getLogger("jobfinder.discovery")
USER_AGENT = "JobFinderDiscovery/1.0 (+personal job search tool; polite, low volume)"
ROBOTS_TOKEN = "JobFinderDiscovery"
ROBOTS_MAX_BYTES = 512_000  # Google reads 500 KiB; a bigger file counts as unreadable (nothing allowed)


def can_fetch(parser: RobotFileParser, useragent: str, url: str) -> bool:
    """RobotFileParser.can_fetch with RFC 9309 precedence: the longest matching rule wins, Allow wins a tie.
    The stdlib applies the first matching rule, so "Disallow: /" listed before "Allow: /api" would block /api
    (Eightfold career sites publish exactly that)."""
    if parser.disallow_all:
        return False
    if parser.allow_all:
        return True
    if not parser.last_checked:
        return False
    p = urlparse(unquote(url))
    path = quote(urlunparse(("", "", p.path, p.params, p.query, p.fragment))) or "/"
    entry = next((e for e in parser.entries if e.applies_to(useragent)), parser.default_entry)
    if entry is None:
        return True
    best = None
    for rule in entry.rulelines:
        if rule.applies_to(path) and (
            best is None or len(rule.path) > len(best.path) or (len(rule.path) == len(best.path) and rule.allowance)
        ):
            best = rule
    return best.allowance if best else True


class RobotsDisallowed(Exception):
    """robots.txt (or an unreadable robots.txt) forbids fetching this URL."""


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT})


class PoliteFetcher:
    """Wraps a client: every GET first consults the host's robots.txt (cached per origin)."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._robots: dict[str, RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}  # one robots.txt fetch per origin under concurrency

    async def _parser(self, origin: str) -> RobotFileParser | None:
        async with self._locks.setdefault(origin, asyncio.Lock()):
            if origin not in self._robots:
                self._robots[origin] = await self._load(origin)
            return self._robots[origin]

    async def _load(self, origin: str) -> RobotFileParser | None:
        """robots.txt goes through safe_fetch like everything else: the origin can come from a pasted URL, so
        even this request is IP-pinned, redirect-checked and capped (a private address -> None -> nothing
        allowed)."""
        accept = (*TEXT_TYPES, "application/octet-stream")  # S3/CDN-hosted robots.txt
        parser = RobotFileParser()
        try:
            r = await safe_fetch(self.client, f"{origin}/robots.txt", headers={"User-Agent": USER_AGENT},
                                 accept=accept, max_bytes=ROBOTS_MAX_BYTES, raise_for_status=False)
        except FetchError:  # private address, unreachable, over the cap or not text: be conservative
            return None
        if r.status in (401, 403):
            parser.disallow_all = True
        elif r.status in (404, 410):
            parser.allow_all = True  # no robots.txt: nothing is forbidden
        elif r.status >= 400:
            return None  # cannot tell: be conservative
        else:
            parser.parse(r.text.splitlines())
        return parser

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        parser = await self._parser(f"{parts.scheme}://{parts.netloc}")
        if parser is None:
            return False
        return can_fetch(parser, ROBOTS_TOKEN, url)

    async def get(self, url: str, **kwargs) -> httpx.Response:
        if not await self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        headers = {"User-Agent": USER_AGENT, **(kwargs.pop("headers", None) or {})}
        r = await self.client.get(url, headers=headers, **kwargs)
        r.raise_for_status()
        return r

    async def post(self, url: str, **kwargs) -> httpx.Response:
        """POST for list endpoints that need a JSON body (Workday); same robots check as get."""
        if not await self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        headers = {"User-Agent": USER_AGENT, **(kwargs.pop("headers", None) or {})}
        r = await self.client.post(url, headers=headers, **kwargs)
        r.raise_for_status()
        return r
