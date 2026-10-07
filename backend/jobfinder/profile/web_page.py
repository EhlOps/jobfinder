"""Fetch a public web page as text (portfolio sites etc.), refusing internal addresses.

Users supply these URLs and the server fetches them, so: every hop is resolved once, checked, and then
connected to *by that validated IP* (no second DNS lookup an attacker could answer differently); the body
is streamed with a byte cap and an overall deadline; and text extraction runs off the event loop."""
import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura

MAX_CHARS = 30_000
MAX_BYTES = 2_000_000
DEADLINE_S = 20.0
MAX_REDIRECTS = 4
USER_AGENT = "Mozilla/5.0 (compatible; JobFinderBot/0.1)"


class FetchError(Exception):
    pass


async def resolve_public(url: str) -> tuple[str, str]:
    """SSRF guard. Returns (hostname, a validated public IP to connect to); raises FetchError otherwise."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchError("URL must be http(s)")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise FetchError("Could not resolve host") from None
    ips = [info[4][0] for info in infos]
    if not ips:
        raise FetchError("Could not resolve host")
    for ip in ips:
        if not ipaddress.ip_address(ip.split("%")[0]).is_global:
            raise FetchError("URL points to a non-public address")
    return parsed.hostname, ips[0]


async def assert_public_url(url: str) -> None:
    await resolve_public(url)


async def _read_capped(resp: httpx.Response) -> bytes:
    chunks, size = [], 0
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if size > MAX_BYTES:
            raise FetchError("Page is too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _fetch(url: str, transport: httpx.AsyncBaseTransport | None) -> str:
    async with httpx.AsyncClient(
        timeout=15, follow_redirects=False, headers={"User-Agent": USER_AGENT}, transport=transport
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            host, ip = await resolve_public(url)
            target = httpx.URL(url).copy_with(host=ip)
            # Connect to the validated IP; keep the real name for the Host header and TLS (SNI + certificate check).
            req = client.build_request("GET", target, headers={"Host": urlparse(url).netloc},
                                       extensions={"sni_hostname": host})
            resp = await client.send(req, stream=True)
            try:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise FetchError("Redirect without a location")
                    url = urljoin(url, location)
                    continue
                if resp.status_code >= 400:
                    raise FetchError(f"HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype and not (ctype.startswith("text/") or ctype in ("application/xhtml+xml", "application/xml")):
                    raise FetchError("That link isn't a web page")
                body = await _read_capped(resp)
                return body.decode(resp.encoding or "utf-8", errors="replace")
            finally:
                await resp.aclose()
        raise FetchError("Too many redirects")


async def fetch_page_text(url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> str:
    try:
        async with asyncio.timeout(DEADLINE_S):
            html = await _fetch(url, transport)
    except TimeoutError:
        raise FetchError("The page took too long to load") from None
    except httpx.HTTPError:
        raise FetchError("Could not fetch the page") from None
    text = await asyncio.to_thread(trafilatura.extract, html) or ""
    return text.strip()[:MAX_CHARS]
