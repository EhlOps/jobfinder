import asyncio

import httpx
import pytest

from jobfinder.profile import web_page

PUBLIC = "93.184.216.34"


def resolver(mapping):
    """Fake DNS: maps hostname -> list of IPs handed out one lookup at a time (last one repeats)."""
    calls: list[str] = []

    async def resolve(url):
        host = httpx.URL(url).host
        ips = mapping[host]
        calls.append(host)
        ip = ips.pop(0) if len(ips) > 1 else ips[0]
        if not __import__("ipaddress").ip_address(ip).is_global:
            raise web_page.FetchError("URL points to a non-public address")
        return host, ip

    return resolve, calls


def page(html="<html><body><article><p>Hello portfolio world, this is a long enough paragraph of text to extract.</p></article></body></html>", **kw):
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=html.encode(), **kw)


async def test_connects_to_the_validated_ip_but_keeps_name_for_host_and_tls(monkeypatch):
    resolve, _ = resolver({"me.dev": [PUBLIC]})
    monkeypatch.setattr(web_page, "resolve_public", resolve)
    seen = {}

    def handler(req: httpx.Request):
        seen.update(host=req.url.host, header=req.headers["host"], sni=req.extensions.get("sni_hostname"))
        return page()

    text = await web_page.fetch_page_text("https://me.dev/about", transport=httpx.MockTransport(handler))
    assert "Hello portfolio world" in text
    assert seen == {"host": PUBLIC, "header": "me.dev", "sni": "me.dev"}


async def test_dns_rebinding_to_a_private_address_is_refused(monkeypatch):
    # public on the first lookup, internal on the next: the redirect hop must be re-checked and refused
    resolve, _ = resolver({"a.dev": [PUBLIC], "evil.dev": ["169.254.169.254"]})
    monkeypatch.setattr(web_page, "resolve_public", resolve)

    def handler(req):
        return httpx.Response(302, headers={"location": "http://evil.dev/latest/meta-data"})

    with pytest.raises(web_page.FetchError, match="non-public"):
        await web_page.fetch_page_text("http://a.dev/", transport=httpx.MockTransport(handler))


async def test_relative_redirects_are_followed_and_loops_stop(monkeypatch):
    resolve, calls = resolver({"a.dev": [PUBLIC]})
    monkeypatch.setattr(web_page, "resolve_public", resolve)
    hops = iter(["/next", None])

    def handler(req):
        nxt = next(hops)
        return httpx.Response(302, headers={"location": nxt}) if nxt else page()

    assert "Hello portfolio" in await web_page.fetch_page_text("http://a.dev/", transport=httpx.MockTransport(handler))
    assert calls == ["a.dev", "a.dev"]  # re-resolved per hop

    with pytest.raises(web_page.FetchError, match="Too many redirects"):
        await web_page.fetch_page_text(
            "http://a.dev/", transport=httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "/x"}))
        )


async def test_oversized_body_is_cut_off(monkeypatch):
    resolve, _ = resolver({"a.dev": [PUBLIC]})
    monkeypatch.setattr(web_page, "resolve_public", resolve)
    monkeypatch.setattr(web_page, "MAX_BYTES", 1000)
    big = httpx.Response(200, headers={"content-type": "text/html"}, content=b"x" * 5000)
    with pytest.raises(web_page.FetchError, match="too large"):
        await web_page.fetch_page_text("http://a.dev/", transport=httpx.MockTransport(lambda r: big))


async def test_slow_drip_hits_the_overall_deadline(monkeypatch):
    resolve, _ = resolver({"a.dev": [PUBLIC]})
    monkeypatch.setattr(web_page, "resolve_public", resolve)
    monkeypatch.setattr(web_page, "DEADLINE_S", 0.3)

    async def drip():
        while True:
            await asyncio.sleep(0.1)
            yield b"x"

    def handler(req):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=drip())

    with pytest.raises(web_page.FetchError, match="too long"):
        await web_page.fetch_page_text("http://a.dev/", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("status, ctype, msg", [(404, "text/html", "HTTP 404"), (200, "application/pdf", "isn't a web page")])
async def test_errors_and_non_pages(monkeypatch, status, ctype, msg):
    resolve, _ = resolver({"a.dev": [PUBLIC]})
    monkeypatch.setattr(web_page, "resolve_public", resolve)
    resp = httpx.Response(status, headers={"content-type": ctype}, content=b"data")
    with pytest.raises(web_page.FetchError, match=msg):
        await web_page.fetch_page_text("http://a.dev/", transport=httpx.MockTransport(lambda r: resp))


async def test_transport_errors_become_fetch_errors(monkeypatch):
    resolve, _ = resolver({"a.dev": [PUBLIC]})
    monkeypatch.setattr(web_page, "resolve_public", resolve)

    def boom(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(web_page.FetchError, match="Could not fetch"):
        await web_page.fetch_page_text("http://a.dev/", transport=httpx.MockTransport(boom))


async def test_every_resolved_address_must_be_public(monkeypatch):
    async def fake_getaddrinfo(self, host, port, **kw):
        return [(2, 1, 6, "", (PUBLIC, 0)), (2, 1, 6, "", ("10.0.0.5", 0))]

    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(web_page.FetchError, match="non-public"):
        await web_page.resolve_public("http://mixed.dev/")
