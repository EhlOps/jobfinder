"""Outbound HTTP basics: the User-Agent JobFinder identifies itself with, the shared client, and the SSRF
guard every fetch of an untrusted URL starts with.

`new_client` builds the one `httpx.AsyncClient` the app context owns (core/context.py). Fetching an
untrusted URL must still go through the SSRF-safe fetch (integrations/http_safety.py), which sends this
User-Agent too and calls `resolve_public` on every hop: the host is resolved once and refused unless every
address is public, and the caller then connects to that validated IP, not to a second DNS answer.
"""
import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

import httpx

USER_AGENT = "JobFinder/0.1 (+personal job search)"
TIMEOUT_S = 20.0


class FetchError(Exception):
    """A fetch was refused or failed. The message is short and safe to record or show."""


def new_client(*, timeout_s: float = TIMEOUT_S, follow_redirects: bool = False,
               transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """A client that sends USER_AGENT. Redirects are off by default: callers check each hop. A shared
    `transport` shares its connection pool (closing the client closes it too)."""
    return httpx.AsyncClient(
        timeout=timeout_s, follow_redirects=follow_redirects, headers={"User-Agent": USER_AGENT},
        transport=transport,
    )


async def resolve_public(url: str) -> tuple[str, str]:
    """SSRF guard. Returns (hostname, a validated public IP to connect to); raises FetchError otherwise."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchError("URL must be http(s)")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise FetchError("Could not resolve host") from None
    ips = [str(info[4][0]) for info in infos]
    if not ips:
        raise FetchError("Could not resolve host")
    for ip in ips:
        if not ipaddress.ip_address(ip.split("%")[0]).is_global:
            raise FetchError("URL points to a non-public address")
    return parsed.hostname, ips[0]
