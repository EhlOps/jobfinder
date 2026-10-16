"""Fetching URLs that came from users or scraped pages, safely.

Every hop (including each redirect) is resolved once, checked to be a public address, and then connected to
*by that validated IP*, so a second DNS answer can't point us at something internal (core/http.py's
resolve_public, as profile/web_page.py uses it). Bodies are streamed with a byte cap, content types are
allow-listed, and robots.txt is honoured when a PoliteFetcher is given. Adapters that build URLs from a host stored in a slug call
assert_public_host() before contacting it.
"""
import asyncio
import json as jsonlib
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

import httpx

from jobfinder.core.http import USER_AGENT, FetchError, resolve_public
from jobfinder.integrations.ats.custom import refusal_for_url

if TYPE_CHECKING:  # robots.py fetches robots.txt through safe_fetch, so it imports this module
    from jobfinder.integrations.robots import PoliteFetcher

MAX_BYTES = 2_000_000
MAX_REDIRECTS = 4
DEADLINE_S = 20.0
TEXT_TYPES = ("text/", "application/xhtml+xml", "application/xml", "application/rss+xml")
JSON_TYPES = ("application/json", "application/ld+json", "text/json")
ANY_TYPES = TEXT_TYPES + JSON_TYPES

__all__ = ["USER_AGENT", "FetchError", "SafeResponse", "assert_public_host", "safe_fetch"]


@dataclass
class SafeResponse:
    url: str  # final URL after redirects
    status: int
    content_type: str
    body: bytes
    encoding: str
    headers: httpx.Headers | None = None  # final response's headers (e.g. Set-Cookie for a retry)

    @property
    def text(self) -> str:
        return self.body.decode(self.encoding or "utf-8", errors="replace")

    def json(self):
        return jsonlib.loads(self.body)


async def assert_public_host(host: str) -> None:
    """Raise FetchError unless every address `host` resolves to is public."""
    if not host or "/" in host or "@" in host:
        raise FetchError("invalid host")
    await resolve_public(f"https://{host}/")


async def _read_capped(resp: httpx.Response, max_bytes: int) -> bytes:
    chunks, size = [], 0
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise FetchError("Response is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _allowed_type(ctype: str, accept: tuple[str, ...]) -> bool:
    return not ctype or any(ctype.startswith(a) if a.endswith("/") else ctype == a for a in accept)


async def _fetch(
    client: httpx.AsyncClient, method: str, url: str, *, headers: dict | None, json, max_bytes: int,
    accept: tuple[str, ...], robots: "PoliteFetcher | None",
) -> SafeResponse:
    for _ in range(MAX_REDIRECTS + 1):
        host, ip = await resolve_public(url)
        if robots is not None and not await robots.allowed(url):
            raise FetchError(f"robots.txt disallows {url}")
        refused = refusal_for_url(url)  # every hop: a redirect must not land on a terms-refused site
        if refused is not None:
            raise FetchError(f"{refused.name}'s terms don't allow automated access ({url})")
        target = httpx.URL(url).copy_with(host=ip)
        hdrs = {"User-Agent": USER_AGENT, **(headers or {}), "Host": urlparse(url).netloc}
        req = client.build_request(method, target, headers=hdrs, json=json, extensions={"sni_hostname": host})
        resp = await client.send(req, stream=True, follow_redirects=False)
        try:
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise FetchError("Redirect without a location")
                url = urljoin(url, location)
                if method != "GET" and resp.status_code in (301, 302, 303):
                    method, json = "GET", None
                continue
            ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if resp.status_code < 400 and not _allowed_type(ctype, accept):
                raise FetchError(f"unexpected content type {ctype!r}")
            body = await _read_capped(resp, max_bytes)
            return SafeResponse(url=url, status=resp.status_code, content_type=ctype, body=body,
                                encoding=resp.encoding or "utf-8", headers=resp.headers)
        finally:
            await resp.aclose()
    raise FetchError("Too many redirects")


async def safe_fetch(
    client: httpx.AsyncClient, url: str, *, method: str = "GET", headers: dict | None = None, json=None,
    max_bytes: int = MAX_BYTES, accept: tuple[str, ...] = ANY_TYPES, robots: "PoliteFetcher | None" = None,
    deadline_s: float = DEADLINE_S, raise_for_status: bool = True,
) -> SafeResponse:
    """GET/POST `url` with SSRF, size, type and (optionally) robots checks on every hop.
    Raises FetchError (never a raw httpx error) so callers can record a readable reason."""
    if urlparse(url).scheme not in ("http", "https"):
        raise FetchError("URL must be http(s)")
    try:
        async with asyncio.timeout(deadline_s):
            r = await _fetch(client, method.upper(), url, headers=headers, json=json, max_bytes=max_bytes,
                             accept=accept, robots=robots)
    except TimeoutError:
        raise FetchError("timed out") from None
    except httpx.HTTPError as e:
        raise FetchError(f"request failed: {type(e).__name__}") from None
    if raise_for_status and r.status >= 400:
        raise FetchError(f"HTTP {r.status}")
    return r
