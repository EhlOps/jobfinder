import httpx
import pytest
from security_payloads import PRIVATE_URLS

from jobfinder.ingest import http_safety
from jobfinder.ingest.http_safety import FetchError, assert_public_host, safe_fetch

PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def public_dns(monkeypatch):
    """Every name resolves to a public IP except ones containing 'internal' (-> 10.0.0.1, refused)."""
    async def fake(url):
        host = httpx.URL(url).host
        if "internal" in host:
            raise FetchError("URL points to a non-public address")
        return host, PUBLIC_IP

    monkeypatch.setattr(http_safety, "resolve_public", fake)


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("url", PRIVATE_URLS)
async def test_private_addresses_refused(url):
    async with client_for(lambda r: httpx.Response(200, text="secret")) as c:
        with pytest.raises(FetchError):
            await safe_fetch(c, url)


async def test_connects_to_validated_ip_with_real_host(public_dns):
    seen = {}

    def handler(request):
        seen["host_url"], seen["host_header"] = request.url.host, request.headers["host"]
        return httpx.Response(200, html="<p>hi</p>")

    async with client_for(handler) as c:
        r = await safe_fetch(c, "https://careers.acme.com/jobs")
    assert r.status == 200 and "hi" in r.text and r.url == "https://careers.acme.com/jobs"
    assert seen == {"host_url": PUBLIC_IP, "host_header": "careers.acme.com"}


async def test_redirect_to_internal_is_refused(public_dns):
    def handler(request):
        if request.headers["host"] == "careers.acme.com":
            return httpx.Response(302, headers={"location": "http://internal.acme.com/admin"})
        return httpx.Response(200, text="should never be read")

    async with client_for(handler) as c:
        with pytest.raises(FetchError, match="non-public"):
            await safe_fetch(c, "https://careers.acme.com/jobs")


async def test_redirects_followed_and_capped(public_dns):
    hops = {"n": 0}

    def handler(request):
        hops["n"] += 1
        return httpx.Response(302, headers={"location": f"/hop{hops['n']}"})

    async with client_for(handler) as c:
        with pytest.raises(FetchError, match="Too many redirects"):
            await safe_fetch(c, "https://careers.acme.com/")
    assert hops["n"] == http_safety.MAX_REDIRECTS + 1


async def test_size_type_and_status_checks(public_dns):
    async with client_for(lambda r: httpx.Response(200, content=b"x" * 5000, headers={"content-type": "text/html"})) as c:
        with pytest.raises(FetchError, match="too large"):
            await safe_fetch(c, "https://a.com/", max_bytes=1000)
    async with client_for(lambda r: httpx.Response(200, content=b"\x00", headers={"content-type": "application/zip"})) as c:
        with pytest.raises(FetchError, match="content type"):
            await safe_fetch(c, "https://a.com/f.zip")
    async with client_for(lambda r: httpx.Response(404, text="nope")) as c:
        with pytest.raises(FetchError, match="404"):
            await safe_fetch(c, "https://a.com/")
        r = await safe_fetch(c, "https://a.com/", raise_for_status=False)
        assert r.status == 404


async def test_json_and_robots(public_dns):
    class NoRobots:
        async def allowed(self, url):
            return "/private" not in url

    async with client_for(lambda r: httpx.Response(200, json={"jobs": [1]})) as c:
        assert (await safe_fetch(c, "https://a.com/api", robots=NoRobots())).json() == {"jobs": [1]}
        with pytest.raises(FetchError, match="robots"):
            await safe_fetch(c, "https://a.com/private", robots=NoRobots())


@pytest.mark.parametrize("bad", ["ftp://a.com/", "file:///etc/passwd", "javascript:alert(1)"])
async def test_non_http_refused(bad):
    async with client_for(lambda r: httpx.Response(200)) as c:
        with pytest.raises(FetchError):
            await safe_fetch(c, bad)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "10.1.2.3", "a.com/x", "user@a.com", ""])
async def test_assert_public_host(host):
    with pytest.raises(FetchError):
        await assert_public_host(host)
