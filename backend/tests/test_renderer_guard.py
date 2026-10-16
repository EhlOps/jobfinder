"""The renderer's sandbox decisions, testable without a browser."""
import pytest

from jobfinder.profile.web_page import FetchError
from jobfinder.renderer.browser import check_top_url, list_spec
from jobfinder.renderer.guard import Blocked, Guard, check_field, check_selector, scheme_reason
from tests.fakes import serve


async def public_only(url):
    """Real resolution for IP literals and localhost; example hosts are 'public' without touching DNS."""
    from jobfinder.profile.web_page import resolve_public

    if url.split("/")[2].endswith(("example.com", "example.org")):
        return "x", "93.184.216.34"
    return await resolve_public(url)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "data:text/html,<script>alert(1)</script>", "javascript:alert(1)",
    "chrome://settings", "chrome-extension://abc/x.js", "blob:https://example.com/uuid", "about:config",
    "ftp://example.com/x", "ws://example.com/socket", "view-source:https://example.com/", "//example.com/x",
    "https:///nohost",
])
def test_non_http_schemes_are_refused(url):
    assert scheme_reason(url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost:8000/admin", "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/", "http://192.168.1.1/", "http://172.17.0.1:5432/", "http://[::1]/", "http://0.0.0.0/",
    "http://[fe80::1]/", "http://100.64.0.1/", "http://[::ffff:127.0.0.1]/",
])
async def test_private_loopback_and_link_local_addresses_are_refused(url):
    g = Guard(public_only)
    assert await g.verdict(url)
    assert g.blocked and url[:30] in g.blocked[0]


@pytest.mark.parametrize("url", ["https://careers.example.com/jobs?q=x", "http://example.org/api/jobs.json"])
async def test_public_http_urls_are_allowed(url):
    assert await Guard(public_only).verdict(url) is None


async def test_verdicts_are_cached_per_host():
    calls = []

    async def counting(url):
        calls.append(url)
        return "x", "93.184.216.34"

    g = Guard(counting)
    for path in ("/a", "/b", "/c.json"):
        assert await g.verdict(f"https://careers.example.com{path}") is None
    assert len(calls) == 1


async def test_unresolvable_host_is_refused():
    async def nxdomain(url):
        raise FetchError("Could not resolve host")

    assert await Guard(nxdomain).verdict("https://nope.invalid/") == "Could not resolve host"


def test_byte_budget():
    g = Guard(max_bytes=100)
    assert g.add_bytes(60) and not g.add_bytes(60)


@pytest.mark.parametrize("sel", [
    "a[href*='/jobs/']", "ul.results > li a.job-link", "div#list .item:nth-child(2n)", "a[data-id^=R-]",
])
def test_plain_css_selectors_pass(sel):
    assert check_selector(sel) == sel


@pytest.mark.parametrize("sel", [
    "", "css=a >> text=Apply", "xpath=//a", "text=Jobs", "js=document.cookie", "a{color:red}", "a;b",
    "a`b`", "a\\x", "a\nb", "a" * 400,
])
def test_engine_syntax_and_odd_selectors_are_refused(sel):
    with pytest.raises(ValueError):
        check_selector(sel)


def test_field_specs():
    assert check_field(":scope") == ":scope"
    assert check_field("@href") == "@href"
    assert check_field(".loc") == ".loc"
    assert check_field("a.title@href") == "a.title@href"
    assert check_field(None) is None
    with pytest.raises(ValueError):
        check_field("xpath=//a@href")


def test_list_spec_checks_the_recipe():
    url, items, fields = list_spec({
        "kind": "rendered_html", "list": {"url": "https://www.widgetco.io/careers/"},
        "items": "a[href*='/careers/positions/']",
        "fields": {"external_id": "@href", "title": ":scope", "url": "https://x.example.com/{external_id}",
                   "location": ".loc"},
    })
    assert url == "https://www.widgetco.io/careers/" and items.startswith("a[")
    assert fields == {"external_id": "@href", "title": ":scope", "location": ".loc"}  # template stays home
    with pytest.raises(ValueError, match="not a rendered_html"):
        list_spec({"kind": "json_api", "list": {"url": "https://a.example.com/"}, "items": "$.x[*]",
                   "fields": {"external_id": "$.id", "title": "$.t", "url": "$.u"}})
    with pytest.raises(ValueError):
        list_spec({"kind": "rendered_html", "list": {"url": "https://a.example.com/"}, "items": "xpath=//a",
                   "fields": {"external_id": "@href", "title": ":scope", "url": "@href"}})
    with pytest.raises(ValueError):  # recipe URLs are http(s) only
        list_spec({"kind": "rendered_html", "list": {"url": "file:///etc/passwd"}, "items": "a",
                   "fields": {"external_id": "@href", "title": ":scope", "url": "@href"}})


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://169.254.169.254/", "http://127.0.0.1:8100/"])
async def test_top_level_url_is_checked_before_any_browser(url):
    with pytest.raises(Blocked):
        await check_top_url(url)


async def test_top_level_url_honours_robots(monkeypatch, web):
    import httpx

    from jobfinder.renderer import browser

    async def fake(url):
        return "careers.example.com", "93.184.216.34"

    monkeypatch.setattr(browser, "resolve_public", fake)
    serve(web, text="User-agent: *\nDisallow: /jobs\n")
    async with httpx.AsyncClient() as client:
        with pytest.raises(Blocked, match="robots"):
            await check_top_url("https://careers.example.com/jobs/1", client)
        await check_top_url("https://careers.example.com/about", client)


def test_browser_module_imports_without_playwright():
    """api/worker images and the test suite never install the renderer extra."""
    import importlib.util

    import jobfinder.renderer.browser  # noqa: F401  (must not import playwright at module level)

    if importlib.util.find_spec("playwright") is None:
        import sys

        assert "playwright" not in sys.modules
