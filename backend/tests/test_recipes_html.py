from pathlib import Path

import httpx
import pytest
from security_payloads import SCRIPT_URLS, SQLI, XSS_TEXT

from jobfinder.ingest import http_safety
from jobfinder.ingest.http_safety import FetchError
from jobfinder.ingest.recipes import RecipeError, run_recipe
from jobfinder.ingest.recipes.html import (
    compile_field,
    compile_selector,
    field_value,
    parse_document,
    select,
)
from tests.fakes import fake_resolve_public, handle

FIXTURES = Path(__file__).parent / "fixtures"
LISTING = (FIXTURES / "recipe_listing.html").read_text()
LIST_URL = "https://careers.acme.com/jobs/"


def recipe(**over):
    base = {
        "kind": "html",
        "list": {"url": LIST_URL},
        "items": "ul.jobs > li.job",
        "fields": {"external_id": "a.job-title@href", "title": "a.job-title", "url": "a.job-title@href",
                   "location": ".location", "posted_at": "time@datetime", "description": "div.summary"},
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(http_safety, "resolve_public", fake_resolve_public())


def site(web, pages: dict[str, str], calls: list | None = None, robots: str | None = None):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots) if robots else httpx.Response(404)
        if calls is not None:
            calls.append(str(request.url))
        key = request.url.params.get("page", "")
        body = pages.get(key)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})

    handle(web, handler)
    return httpx.AsyncClient()


def doc(html):
    return parse_document(html)


def test_selector_subset_matches_like_css():
    d = doc(LISTING)
    assert len(select(d, compile_selector("ul.jobs > li.job"))) == 5
    assert len(select(d, compile_selector("li.job.featured a"))) == 1
    assert len(select(d, compile_selector("ul > a"))) == 0  # child, not descendant
    assert len(select(d, compile_selector("a[href^='/jobs/4']"))) == 1
    assert len(select(d, compile_selector('a[href*="/jobs/"]'))) == 4
    assert len(select(d, compile_selector("a[href$=list]"))) == 1
    assert len(select(d, compile_selector("li[class~=featured]"))) == 1
    assert len(select(d, compile_selector("meta[property='og:site_name']"))) == 1
    assert len(select(d, compile_selector("time[datetime]"))) == 2
    assert len(select(d, compile_selector("* .location"))) == 4
    item = select(d, compile_selector("li.job"))[0]
    assert field_value(item, compile_field("a.job-title@href")) == "/jobs/4021-robotics-engineer"
    assert field_value(item, compile_field(".location")) == "Boston, MA"
    assert field_value(item, compile_field("time@datetime")) == "2026-10-01"
    a = select(item, compile_selector("a"))[0]
    assert field_value(a, compile_field(":scope")) == "Robotics Engineer"
    assert field_value(a, compile_field("@href")) == "/jobs/4021-robotics-engineer"
    assert field_value(item, compile_field("a[data-x='a@b']")) == ""  # '@' inside [..] isn't an attr suffix


@pytest.mark.parametrize("bad", [
    "a:contains('x')", "a:not(.x)", "//a", "a | b", "a, b", "count(//a)", "concat('a','b')",
    "a[href=\"x\" or 1=1]", "a[x='y'z']", "a[x=y\\]]", "a >", "> a", "a[", "a]", "a::before", "a + b",
    "a ~ b", "*div", "a/b", "text()", "a[@href]", "$.a", ":root", "a{}", "a;b",
    *SQLI, *XSS_TEXT, *SCRIPT_URLS,
])
def test_selectors_outside_the_subset_are_refused(bad):
    with pytest.raises(RecipeError):
        compile_selector(bad)


async def test_bad_selector_refused_before_any_request(web):
    calls = []
    async with site(web, {"": LISTING}, calls) as c:
        with pytest.raises(RecipeError):
            await run_recipe(c, "acme.com", "Acme", recipe(items="li:has(script)"))
        with pytest.raises(RecipeError):
            await run_recipe(c, "acme.com", "Acme", recipe(fields={"external_id": "a@href", "title": "a)",
                                                                   "url": "a@href"}))
    assert calls == []


async def test_html_runner_maps_fields(web):
    async with site(web, {"": LISTING}) as c:
        jobs = await run_recipe(c, "acme.com", "Acme", recipe())
    by = {j.title: j for j in jobs}
    assert list(by) == ["Robotics Engineer", "Controls Engineer", "Senior Test Engineer",
                        "Sneaky <script>alert(1)</script>"]  # text, never markup (React escapes it)
    r = by["Robotics Engineer"]
    assert r.source == "recipe" and r.external_id == "acme.com:/jobs/4021-robotics-engineer"
    assert r.url == "https://careers.acme.com/jobs/4021-robotics-engineer"
    assert r.location == "Boston, MA" and r.posted_at.year == 2026 and r.posted_at.tzinfo is not None
    assert "Build robots" in r.description_text and "<p>" not in r.description_text
    assert r.salary_min == 120000 and r.salary_max == 150000
    assert by["Controls Engineer"].workplace_type == "remote"
    assert by["Controls Engineer"].external_id == "acme.com:/jobs/4022-controls-engineer"  # absolute == relative
    t = by["Senior Test Engineer"]
    assert t.url == "https://careers.acme.com/jobs/jobs/4023-test-engineer?ref=list"  # urljoin'd
    assert t.seniority == "senior" and t.posted_at is None
    assert by["Sneaky <script>alert(1)</script>"].url == ""  # javascript: never becomes a link


def test_description_excludes_following_text():
    d = doc('<div class="job"><p class="desc">Build things</p> Apply by Friday | Share</div>')
    item = select(d, compile_selector("div.job"))[0]
    assert "Friday" not in field_value(item, compile_field("p.desc"), as_html=True)


def test_page_charset_is_honoured():
    from jobfinder.ingest.http_safety import SafeResponse
    from jobfinder.ingest.recipes.html import page_text

    body = '<meta charset="windows-1252"><p>Café</p>'.encode("cp1252")
    assert "Café" in page_text(SafeResponse("https://x/", 200, "text/html", body, "utf-8", httpx.Headers()))
    hdr = httpx.Headers({"content-type": "text/html; charset=iso-8859-1"})
    assert "Café" in page_text(SafeResponse("https://x/", 200, "text/html", "Café".encode("latin-1"), "", hdr))
    utf = "Zürich".encode()
    assert page_text(SafeResponse("https://x/", 200, "text/html", utf, "utf-8", None)) == "Zürich"
    bogus = b'<meta charset="no-such-codec">ok'
    assert "ok" in page_text(SafeResponse("https://x/", 200, "text/html", bogus, "utf-8", None))


def test_nested_descendant_matches_are_not_duplicated():
    d = doc("<div><div><div><a href='/1'>x</a></div></div></div>")
    assert len(select(d, compile_selector("div div a"))) == 1
    assert len(select(d, compile_selector("div div"))) == 2


async def test_url_template_and_scope_fields(web):
    r = recipe(items="a.job-title", fields={"external_id": "@href", "title": ":scope",
                                            "url": "/apply?job={external_id}"})
    async with site(web, {"": LISTING}) as c:
        jobs = await run_recipe(c, "acme.com", "Acme", r)
    assert jobs[0].url == "https://careers.acme.com/apply?job=/jobs/4021-robotics-engineer"


@pytest.mark.parametrize("href", SCRIPT_URLS)
async def test_script_hrefs_never_become_links(href, web):
    page = f'<ul class="jobs"><li class="job"><a class="job-title" href="{href}">Engineer</a></li></ul>'
    async with site(web, {"": page}) as c:
        jobs = await run_recipe(c, "acme.com", "Acme", recipe())
    assert len(jobs) == 1
    if href.startswith("//"):  # protocol-relative: a real (if foreign) http(s) link
        assert jobs[0].url == "https://evil.example/x"
    else:
        assert jobs[0].url == ""


def _page(n):
    lis = "".join(f'<li class="job"><a class="job-title" href="/jobs/{n}{i}">Job {n}{i}</a></li>'
                  for i in range(2))
    return f'<ul class="jobs">{lis}</ul>'


async def test_pagination_stops_on_empty_and_repeating_pages(web):
    calls = []
    pg = {"kind": "page", "param": "page", "start": 1, "max_pages": 10}
    async with site(web, {"1": _page(1), "2": _page(2), "3": "<p>no jobs</p>"}, calls) as c:
        jobs = await run_recipe(c, "acme.com", "Acme", recipe(pagination=pg))
    assert len(jobs) == 4 and len(calls) == 3
    calls.clear()
    async with site(web, {"1": _page(1), "2": _page(1)}, calls) as c:  # site ignores ?page: same page again
        jobs = await run_recipe(c, "acme.com", "Acme", recipe(pagination=pg))
    assert len(jobs) == 2 and len(calls) == 2


async def test_robots_disallow_is_honoured(web):
    async with site(web, {"": LISTING}, robots="User-agent: *\nDisallow: /jobs/\n") as c:
        with pytest.raises(FetchError, match="robots"):
            await run_recipe(c, "acme.com", "Acme", recipe())


async def test_huge_page_is_capped(web):
    async with site(web, {"": "<p>" + "x" * 3_100_000 + "</p>"}) as c:
        with pytest.raises(FetchError, match="too large"):
            await run_recipe(c, "acme.com", "Acme", recipe())


async def test_non_html_content_refused(web):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})

    handle(web, handler)
    async with httpx.AsyncClient() as c:
        with pytest.raises(FetchError, match="content type"):
            await run_recipe(c, "acme.com", "Acme", recipe())


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://10.0.0.5/admin", "http://[::1]/"])
async def test_private_list_urls_are_never_fetched(url, monkeypatch, web):
    monkeypatch.undo()  # real resolve_public: literal private IPs are refused without DNS
    calls = []
    async with site(web, {"": LISTING}, calls) as c:
        with pytest.raises(FetchError):
            await run_recipe(c, "acme.com", "Acme", recipe(list={"url": url}))
    assert calls == []


async def test_empty_and_comment_only_pages_yield_nothing(web):
    for body in ("", "<!-- nothing -->", "   "):
        async with site(web, {"": body}) as c:
            assert await run_recipe(c, "acme.com", "Acme", recipe()) == []
