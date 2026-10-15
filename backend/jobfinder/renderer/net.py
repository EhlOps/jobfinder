"""All of the renderer's network traffic, done from Python and pinned to checked addresses.

Chromium itself is pointed at a dead proxy, so nothing it tries on its own (preconnects, prefetches, a
request that slipped past the route handler) reaches the network. Every request the page makes is answered by
the route handler from here. `PinnedTransport` resolves each hop's host once, refuses it unless every address
is public (profile/web_page.resolve_public), then connects to *that* IP with the real name kept for the Host
header and TLS, so a second DNS answer can't point the request somewhere internal (no DNS rebinding).
"""
import contextlib
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from jobfinder.core.http import USER_AGENT
from jobfinder.ingest.discovery.robots import ROBOTS_TOKEN, can_fetch
from jobfinder.profile import web_page
from jobfinder.renderer.guard import Resolver

ROBOTS_MAX_BYTES = 500_000
# Hop-by-hop or recomputed headers: never forwarded from the browser, never handed back to it.
_DROP_REQUEST = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "te", "upgrade",
                 "accept-encoding", "proxy-authorization", "proxy-connection"}
_DROP_RESPONSE = {"content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive", "alt-svc"}


class TooLarge(Exception):
    pass


class PinnedTransport(httpx.AsyncBaseTransport):
    def __init__(self, resolver: Resolver | None = None, inner: httpx.AsyncBaseTransport | None = None):
        self._resolver = resolver
        self._inner = inner or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme not in ("http", "https"):
            raise httpx.UnsupportedProtocol("only http(s)", request=request)
        resolve = self._resolver or web_page.resolve_public
        try:
            host, ip = await resolve(str(request.url))
        except web_page.FetchError as e:
            raise httpx.ConnectError(f"refused: {e}", request=request) from None
        request.headers["Host"] = request.url.netloc.decode("ascii")
        request.url = request.url.copy_with(host=ip)
        request.extensions = {**request.extensions, "sni_hostname": host}
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def make_client(resolver: Resolver | None = None, inner: httpx.AsyncBaseTransport | None = None, *,
                follow_redirects: bool = False, timeout: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=PinnedTransport(resolver, inner), follow_redirects=follow_redirects,
                             timeout=timeout, headers={"User-Agent": USER_AGENT})


def request_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _DROP_REQUEST and not k.startswith(":")}


def response_headers(resp: httpx.Response) -> dict[str, str]:
    """Headers to hand the browser. Repeated Set-Cookie headers are joined with newlines (Playwright's
    convention); any other repeated header with commas, as HTTP allows. A newline anywhere else makes Chromium
    drop the whole response silently."""
    out: dict[str, str] = {}
    for k, v in resp.headers.multi_items():
        k = k.lower()
        if k in _DROP_RESPONSE or "\n" in v or "\r" in v:
            continue
        sep = "\n" if k == "set-cookie" else ", "
        out[k] = f"{out[k]}{sep}{v}" if k in out else v
    return out


async def read_capped(resp: httpx.Response, max_bytes: int) -> bytes:
    chunks, size = [], 0
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise TooLarge(f"response over {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def robots_allowed(client: httpx.AsyncClient, url: str) -> bool:
    """robots.txt for `url`'s origin, fetched through the pinned transport (redirects followed, each hop
    checked). Same reading as PoliteFetcher: 401/403 forbid all, 404/410 allow all, other errors forbid."""
    p = urlsplit(url)
    parser = RobotFileParser()
    try:
        async with client.stream("GET", f"{p.scheme}://{p.netloc}/robots.txt", follow_redirects=True) as r:
            if r.status_code in (401, 403):
                return False
            if r.status_code in (404, 410):
                return True
            if r.status_code >= 400:
                return False
            if not r.headers.get("content-type", "text/plain").lower().startswith("text/plain"):
                return True  # redirected to a web page (no robots.txt there): nothing is forbidden
            body = b""
            async for chunk in r.aiter_bytes():  # RFC 9309: rules past the first 500 KiB may be ignored
                body += chunk
                if len(body) >= ROBOTS_MAX_BYTES:
                    break
    except httpx.HTTPError:
        return False
    with contextlib.suppress(Exception):
        parser.parse(body.decode("utf-8", errors="replace").splitlines())
        parser.modified()
        return can_fetch(parser, ROBOTS_TOKEN, url)
    return False
