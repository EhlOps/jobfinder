"""robots.txt is fetched through safe_fetch: IP-pinned, every redirect re-checked, size-capped."""
import httpx
import pytest
from security_payloads import PRIVATE_URLS

from jobfinder.ingest import http_safety
from jobfinder.ingest.discovery.robots import ROBOTS_MAX_BYTES, USER_AGENT, PoliteFetcher
from jobfinder.ingest.resolve.ladder import ResolveContext
from tests.fakes import PUBLIC_IP, fake_resolve_public, handle, serve


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(http_safety, "resolve_public", fake_resolve_public(refuse_internal=True, refuse_private=True))


def recorder(web, routes: dict[str, httpx.Response], seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("host"), request.url.path))
        return routes.get(f"{request.headers.get('host')}{request.url.path}", httpx.Response(404))

    handle(web, handler)
    return httpx.AsyncClient()


@pytest.mark.parametrize("url", PRIVATE_URLS)
async def test_robots_on_a_private_address_is_never_fetched(url, web):
    seen: list = []
    async with recorder(web, {}, seen) as c:
        assert not await PoliteFetcher(c).allowed(url)
    assert seen == []


async def test_robots_redirecting_to_a_private_address_counts_as_disallowed(web):
    seen: list = []
    routes = {"jobs.acme.com/robots.txt": httpx.Response(302, headers={"location": "http://internal.acme.local/robots.txt"})}
    async with recorder(web, routes, seen) as c:
        assert not await PoliteFetcher(c).allowed("https://jobs.acme.com/careers")
    assert [s[1] for s in seen] == ["jobs.acme.com"]  # the internal hop was refused before connecting


async def test_robots_is_fetched_by_validated_ip_with_the_discovery_agent(web):
    seen: list = []
    agents: list = []

    def handler(request):
        agents.append(request.headers["user-agent"])
        seen.append(request.url.host)
        return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")

    handle(web, handler)
    async with httpx.AsyncClient() as c:
        f = PoliteFetcher(c)
        assert await f.allowed("https://jobs.acme.com/careers")
        assert not await f.allowed("https://jobs.acme.com/private/x")
    assert seen == [PUBLIC_IP] and agents == [USER_AGENT]  # fetched once, by IP


@pytest.mark.parametrize("status,allowed", [(401, False), (403, False), (404, True), (410, True), (500, False)])
async def test_robots_status_mapping_is_unchanged(status, allowed, web):
    serve(web, status=status)
    async with httpx.AsyncClient() as c:
        assert await PoliteFetcher(c).allowed("https://jobs.acme.com/x") is allowed


async def test_oversized_or_non_text_robots_is_conservative(web):
    serve(web, text="#" * (ROBOTS_MAX_BYTES + 10))
    async with httpx.AsyncClient() as c:
        assert not await PoliteFetcher(c).allowed("https://jobs.acme.com/x")
    serve(web, content=b"\x89PNG", headers={"content-type": "image/png"})
    async with httpx.AsyncClient() as c:
        assert not await PoliteFetcher(c).allowed("https://jobs.acme.com/x")
    serve(web, content=b"User-agent: *\nDisallow: /a/\n", headers={"content-type": "application/octet-stream"})
    async with httpx.AsyncClient() as c:
        f = PoliteFetcher(c)
        assert await f.allowed("https://jobs.acme.com/x") and not await f.allowed("https://jobs.acme.com/a/1")


async def test_pasted_page_with_private_robots_redirect_is_not_fetched(web):
    seen: list = []
    routes = {"careers.acme.com/robots.txt": httpx.Response(301, headers={"location": "http://127.0.0.1:8080/robots.txt"}),
              "careers.acme.com/jobs/1": httpx.Response(200, html="<html>job</html>")}
    async with recorder(web, routes, seen) as c:
        ctx = ResolveContext("https://careers.acme.com/jobs/1", c)
        assert await ctx.page() is None
    assert "robots" in ctx.page_error and all(path == "/robots.txt" for _, _, path in seen)
