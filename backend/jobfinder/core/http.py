"""Outbound HTTP basics: the User-Agent JobFinder identifies itself with, and the shared client.

`new_client` builds the one `httpx.AsyncClient` the app context owns (core/context.py). Fetching an
untrusted URL must still go through the SSRF-safe fetch (ingest/http_safety.py), which sends this
User-Agent too.
"""
import httpx

USER_AGENT = "JobFinder/0.1 (+personal job search)"
TIMEOUT_S = 20.0


def new_client(*, timeout_s: float = TIMEOUT_S, follow_redirects: bool = False) -> httpx.AsyncClient:
    """A client that sends USER_AGENT. Redirects are off by default: callers check each hop."""
    return httpx.AsyncClient(
        timeout=timeout_s, follow_redirects=follow_redirects, headers={"User-Agent": USER_AGENT}
    )
