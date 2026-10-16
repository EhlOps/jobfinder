"""The renderer's own network path (no browser needed): every hop pinned to a checked public IP."""
import httpx
import pytest

from jobfinder.profile.web_page import FetchError
from jobfinder.renderer import net
from tests.fakes import handle

PUBLIC = "93.184.216.34"


async def fake_dns(url):
    host = httpx.URL(url).host
    if host in ("internal.example.com", "127.0.0.1", "169.254.169.254"):
        raise FetchError("URL points to a non-public address")
    return host, PUBLIC


def recorder(web, handler):
    seen = []

    def wrapped(request: httpx.Request):
        seen.append(request)
        return handler(request)

    handle(web, wrapped)
    return seen


async def test_connects_to_the_checked_ip_with_the_real_host_and_sni(web):
    seen = recorder(web, lambda r: httpx.Response(200, json={"ok": True}))
    async with net.make_client(fake_dns) as client:
        r = await client.get("https://careers.example.com/api/jobs?x=1")
    assert r.json() == {"ok": True}
    req = seen[0]
    assert req.url.host == PUBLIC and req.url.path == "/api/jobs" and req.url.query == b"x=1"
    assert req.headers["Host"] == "careers.example.com"
    assert req.extensions["sni_hostname"] == "careers.example.com"


async def test_refuses_non_public_hosts_before_connecting(web):
    seen = recorder(web, lambda r: httpx.Response(200))
    async with net.make_client(fake_dns) as client:
        for url in ("http://127.0.0.1:8100/api/health", "http://169.254.169.254/latest/meta-data/",
                    "https://internal.example.com/"):
            with pytest.raises(httpx.ConnectError, match="refused"):
                await client.get(url)
    assert seen == []


async def test_every_redirect_hop_is_checked(web):
    def handler(r: httpx.Request):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    seen = recorder(web, handler)
    async with net.make_client(fake_dns, follow_redirects=True) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("https://careers.example.com/go")
    assert len(seen) == 1  # the internal hop never left the box


async def test_read_capped(web):
    recorder(web, lambda r: httpx.Response(200, content=b"x" * 5000))
    async with net.make_client(fake_dns) as client, \
            client.stream("GET", "https://careers.example.com/big") as r:
        with pytest.raises(net.TooLarge):
            await net.read_capped(r, 1000)


def test_header_filtering():
    out = net.request_headers({"Host": "evil", "cookie": "a=1", "accept-encoding": "br", "user-agent": "UA",
                               ":authority": "x", "Content-Length": "9"})
    assert out == {"cookie": "a=1", "user-agent": "UA"}
    resp = httpx.Response(200, headers=[("content-type", "text/html"), ("set-cookie", "a=1"),
                                        ("set-cookie", "b=2"), ("content-encoding", "gzip"),
                                        ("Vary", "Accept-Encoding"), ("vary", "Cookie")])
    assert net.response_headers(resp) == {"content-type": "text/html", "set-cookie": "a=1\nb=2",
                                          "vary": "Accept-Encoding, Cookie"}  # a newline here breaks Chromium


@pytest.mark.parametrize("status,body,allowed", [
    (200, "User-agent: *\nDisallow: /jobs\n", False),
    (200, "User-agent: *\nDisallow: /private\n", True),
    (404, "", True), (403, "", False), (500, "", False),
])
async def test_robots(web, status, body, allowed):
    seen = recorder(web, lambda r: httpx.Response(status, text=body))
    async with net.make_client(fake_dns) as client:
        assert await net.robots_allowed(client, "https://careers.example.com/jobs/1") is allowed
    assert str(seen[0].url).endswith("/robots.txt")


async def test_robots_that_is_really_a_web_page_allows(web):
    recorder(web, lambda r: httpx.Response(200, html="<html>" + "x" * 600_000 + "</html>"))
    async with net.make_client(fake_dns) as client:
        assert await net.robots_allowed(client, "https://careers.example.com/jobs/1") is True


async def test_robots_redirect_to_internal_is_refused(web):
    seen = recorder(web, lambda r: httpx.Response(301, headers={"location": "http://127.0.0.1/robots.txt"}))
    async with net.make_client(fake_dns) as client:
        assert await net.robots_allowed(client, "https://careers.example.com/jobs/1") is False
    assert len(seen) == 1
