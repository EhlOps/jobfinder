import json
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from urllib.robotparser import RobotFileParser

import httpx
import pytest
from security_payloads import PRIVATE_URLS, SCRIPT_URLS, SQLI, XSS_TEXT

from jobfinder.integrations import http_safety
from jobfinder.integrations.ats import eightfold as ef
from jobfinder.integrations.ats.detect import HTML_DETECTORS, detect_board
from jobfinder.integrations.ats.eightfold import (
    detect_html,
    fetch_eightfold,
    parse_eightfold,
    parse_slug,
)
from jobfinder.integrations.ats.registry import ATS_FETCHERS, PROBE_FETCHERS
from jobfinder.integrations.directories import board_from_url
from jobfinder.integrations.http_safety import FetchError
from jobfinder.integrations.robots import ROBOTS_TOKEN, PoliteFetcher, can_fetch
from jobfinder.services.discovery import _PoliteClient
from tests.fakes import fake_resolve_public, handle

FIX = Path(__file__).parent / "fixtures"
PCSX_LIST = json.loads((FIX / "eightfold_pcsx_search.json").read_text())
PCSX_DETAIL = json.loads((FIX / "eightfold_pcsx_detail.json").read_text())
V2_LIST = json.loads((FIX / "eightfold_v2_jobs.json").read_text())
V2_DETAIL = json.loads((FIX / "eightfold_v2_detail.json").read_text())
PAGE_HTML = (FIX / "eightfold_job_page.html").read_text()
MS = "apply.careers.microsoft.com__microsoft.com"
NF = "explore.jobs.netflix.net__netflix.com"
# robots.txt as served by apply.careers.microsoft.com and explore.jobs.netflix.net (2026-10)
ROBOTS = """User-agent: *
Disallow: /
Allow: /$
Allow: /careers
Allow: /api/apply
Allow: /api/pcsx
Allow: /candidate/login
Allow: /login
Allow: /api/events
Allow: /static/gen
"""


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Every name resolves to a public IP except ones containing 'internal'; no real sleeping; fresh API cache."""
    fake = fake_resolve_public(refuse_internal=True)

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(http_safety, "resolve_public", fake)
    monkeypatch.setattr(ef, "_sleep", no_sleep)
    ef._api_for.clear()


def test_registered():
    assert ATS_FETCHERS["eightfold"] is fetch_eightfold
    assert isinstance(PROBE_FETCHERS["eightfold"], partial)
    assert PROBE_FETCHERS["eightfold"].keywords == {"list_only": True}
    assert detect_html in HTML_DETECTORS


# ── slugs and URL recognition ─────────────────────────────────────────────
def test_parse_slug():
    assert parse_slug(MS) == ("apply.careers.microsoft.com", "microsoft.com")
    assert parse_slug("netflix.eightfold.ai__netflix.com") == ("netflix.eightfold.ai", "netflix.com")


@pytest.mark.parametrize("bad", [
    "", "microsoft.com", "a.com__b.com__c.com", "Apply.Careers.Microsoft.com__microsoft.com",
    "localhost__microsoft.com", "10.0.0.1__microsoft.com", "a.com__127.0.0.1", "a.com__", "__a.com",
    "a.com/x__b.com", "a.com__b.com/../x", "a.com:8080__b.com", "a.com__x.eightfold.ai", "-a.com__b.com",
    *[f"a.com__{s}" for s in SQLI], *[f"{s}__b.com" for s in SQLI],
])
def test_parse_slug_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_slug(bad)


@pytest.mark.parametrize("url,expected", [
    ("https://apply.careers.microsoft.com/careers/job/1970393557008022?domain=microsoft.com", MS),
    ("https://apply.careers.microsoft.com/careers/job/1970393557008022/?domain=microsoft.com&hl=en", MS),
    ("https://explore.jobs.netflix.net/careers/job/790318907811-lead?domain=netflix.com&microsite=netflix.com", NF),
    ("https://EXPLORE.jobs.netflix.net/careers/job/790318907811?domain=NETFLIX.com", NF),
    ("https://netflix.eightfold.ai/careers?domain=netflix.com", "netflix.eightfold.ai__netflix.com"),
    ("https://netflix.eightfold.ai/careers/job/790318907811", "netflix.eightfold.ai__netflix.com"),
    ("https://acme.eightfold.ai/careers", "acme.eightfold.ai__acme.com"),  # validation must confirm acme.com
    ("https://acme.eightfold.ai/", "acme.eightfold.ai__acme.com"),
])
def test_board_from_url(url, expected):
    assert board_from_url(url) == ("eightfold", expected)


@pytest.mark.parametrize("url", [
    "https://explore.jobs.netflix.net/careers/job/790318907811",  # custom host without ?domain=: HTML rung
    "https://careers.acme.com/careers/job/abc?domain=acme.com",  # not a numeric position id
    "https://careers.acme.com/jobs/123?domain=acme.com",
    "https://careers.acme.com/careers/job/123?domain=",
    "https://careers.acme.com/careers/job/123?domain=acme",
    "https://app.eightfold.ai/careers?domain=acme.com",
    "https://www.eightfold.ai/careers",
    "https://a.b.eightfold.ai/careers",
    "https://acme.eightfold.ai/api/apply/v2/jobs?domain=acme.com",
    "ftp://careers.acme.com/careers/job/123?domain=acme.com",
    *[f"https://careers.acme.com/careers/job/123?domain={s}" for s in SQLI],
    *[f"{u.rstrip('/')}/careers/job/123?domain=acme.com" for u in PRIVATE_URLS],
])
def test_board_from_url_rejects(url):
    assert board_from_url(url) is None


# ── HTML detection ─────────────────────────────────────────────────────────
def test_detect_html_custom_domain():
    url = "https://explore.jobs.netflix.net/careers/job/790318907811"
    assert detect_html(url, PAGE_HTML) == ("eightfold", NF)
    assert detect_board(url, PAGE_HTML) == ("eightfold", NF)


def test_detect_html_prefers_the_page_urls_domain_and_ignores_eightfold_domains():
    html = '<script src="https://static.vscdn.net/x.js"></script>' + '"domain": "eightfolddemo-x.com"' * 5
    assert detect_html("https://jobs.acme.com/careers?domain=acme.com", html) == ("eightfold", "jobs.acme.com__acme.com")
    assert detect_html("https://jobs.acme.com/careers", html) is None
    assert detect_html("https://jobs.acme.com/careers", html + '<a href="/careers?domain=acme.com">') == (
        "eightfold", "jobs.acme.com__acme.com")


@pytest.mark.parametrize("url,html", [
    ("https://jobs.acme.com/careers", '<a href="/careers?domain=acme.com">no eightfold markers</a>'),
    ("https://jobs.acme.com/careers", ""),
    ("https://10.0.0.1/careers", PAGE_HTML),
    ("javascript:alert(1)", PAGE_HTML),
    ("https://jobs.acme.com/careers?domain=acme';DROP TABLE companies;--", '<link href="//static.vscdn.net/a">'),
])
def test_detect_html_rejects(url, html):
    assert detect_html(url, html) is None


# ── parsing ───────────────────────────────────────────────────────────────
def test_parse_pcsx_with_detail():
    items = PCSX_LIST["data"]["positions"]
    items = [{**items[0], "detail": PCSX_DETAIL["data"]}, *items[1:]]
    a, b, c = parse_eightfold(items, "Microsoft", MS)
    assert a.source == "eightfold" and a.external_id == "microsoft.com:1970393557008022"
    assert a.title == "Retail Partner Marketing Manager" and a.company_name == "Microsoft"
    assert a.url == "https://apply.careers.microsoft.com/careers/job/1970393557008022"
    assert a.location == "United States, Multiple Locations, Multiple Locations"
    assert a.posted_at == datetime.fromtimestamp(1791940000, UTC) and a.workplace_type == "onsite"
    assert a.employment_type == "Full-Time"
    assert "MCAPS" in a.description_text and "<p>" not in a.description_text
    assert (a.salary_min, a.salary_max, a.salary_currency) == (106400, 203600, "USD")
    # no detail: listing data only; several locations joined; URL built from the id
    assert b.description_text == "" and "Redmond" in b.location and "Fargo" in b.location
    assert b.url == "https://apply.careers.microsoft.com/careers/job/1970393557040349?domain=microsoft.com"
    assert c.seniority == "senior"


def test_parse_v2_with_detail():
    items = V2_LIST["positions"]
    items = [{**items[0], "detail": V2_DETAIL}, *items[1:]]
    a, _, c = parse_eightfold(items, "Netflix", NF)
    assert a.external_id == "netflix.com:790318907811"
    assert a.url == "https://explore.jobs.netflix.net/careers/job/790318907811?microsite=netflix.com"
    assert a.location == "New York,New York,United States of America"
    assert a.posted_at == datetime.fromtimestamp(1791763200, UTC)
    # the detail's null work_location_option must not wipe the listing's "onsite"
    assert a.workplace_type == "onsite" and "Netflix" in a.description_text
    assert c.workplace_type == "remote"  # "remote_local"


def test_parse_skips_bad_items_and_neutralises_hostile_fields():
    items = [
        {"id": 1, "name": " "},
        {"name": "No id"},
        {"id": "12; DROP TABLE jobs", "name": "Bad id"},
        *[{"id": 100 + i, "name": "Engineer", "publicUrl": u, "canonicalPositionUrl": u} for i, u in enumerate(SCRIPT_URLS)],
        {"id": 200, "name": XSS_TEXT[0], "job_description": XSS_TEXT[1] + "<p>ok</p>", "t_create": "nan"},
        {"id": 201, "name": "Ms timestamp", "postedTs": 1791940000000},
    ]
    jobs = parse_eightfold(items, "Acme", "jobs.acme.com__acme.com")
    assert [j.external_id for j in jobs] == [f"acme.com:{100 + i}" for i in range(len(SCRIPT_URLS))] + [
        "acme.com:200", "acme.com:201"]
    for j in jobs:
        assert j.url.startswith("https://jobs.acme.com/careers/job/") and "script" not in j.url.lower()
    assert "<" not in jobs[-2].description_text and jobs[-2].posted_at is None and jobs[-1].posted_at is None


# ── fetching ──────────────────────────────────────────────────────────────
def _site(pcsx=True, total=None, robots=ROBOTS, fail_details=False, calls=None, rate_limit=False):
    """A fake Eightfold host; PCSX on or off, `total` synthetic positions (default: the fixture's 3)."""
    state = {"limited": rate_limit}

    def positions(src, start):
        if total is None:
            return src[start:start + 10] if start < len(src) else []
        return [{**src[0], "id": 1000 + i, "name": f"Engineer {i}"} for i in range(start, min(start + 10, total))]

    def handler(req: httpx.Request) -> httpx.Response:
        host, path, q = req.headers["host"], req.url.path, req.url.params
        if calls is not None:
            calls.append(path)
        if path == "/robots.txt":
            return httpx.Response(200, text=robots)
        assert host in ("apply.careers.microsoft.com", "explore.jobs.netflix.net"), host
        if state["limited"] and req.headers.get("cookie") != "_vs=abc":
            return httpx.Response(429, text="Please try again later", headers={"set-cookie": "_vs=abc; Path=/"})
        if path == "/api/pcsx/search":
            if not pcsx:
                return httpx.Response(403, json={"message": "PCSX is not enabled for this user."})
            assert q["domain"] == "microsoft.com" and q["sort_by"] == "timestamp"
            src = PCSX_LIST["data"]["positions"]
            data = {**PCSX_LIST["data"], "positions": positions(src, int(q["start"])),
                    "count": total if total is not None else len(src)}
            return httpx.Response(200, json={**PCSX_LIST, "data": data})
        if path == "/api/pcsx/position_details":
            if fail_details:
                return httpx.Response(500)
            return httpx.Response(200, json=PCSX_DETAIL)
        if path == "/api/apply/v2/jobs":
            assert q["domain"] == "netflix.com" and q["num"] == "10"
            src = V2_LIST["positions"]
            return httpx.Response(200, json={**V2_LIST, "positions": positions(src, int(q["start"])),
                                             "count": total if total is not None else len(src)})
        if path.startswith("/api/apply/v2/jobs/"):
            if fail_details:
                return httpx.Response(500)
            return httpx.Response(200, json={**V2_DETAIL, "id": path.rsplit("/", 1)[-1]})
        return httpx.Response(404)

    return handler


def client_for(web, handler):
    handle(web, handler)
    return httpx.AsyncClient()


async def test_fetch_pcsx_with_details(web):
    calls: list = []
    async with client_for(web, _site(calls=calls)) as c:
        jobs = await fetch_eightfold(c, MS, "Microsoft")
    assert [j.external_id for j in jobs] == [f"microsoft.com:{p['id']}" for p in PCSX_LIST["data"]["positions"]]
    assert all("MCAPS" in j.description_text for j in jobs)
    assert calls.count("/robots.txt") == 1 and "/api/apply/v2/jobs" not in calls
    assert ef._api_for[MS] == "pcsx"


async def test_fetch_falls_back_to_v2_when_pcsx_is_disabled(web):
    calls: list = []
    async with client_for(web, _site(pcsx=False, calls=calls)) as c:
        jobs = await fetch_eightfold(c, NF, "Netflix")
        assert len(jobs) == 3 and all("Netflix" in j.description_text for j in jobs)
        assert ef._api_for[NF] == "v2"
        calls.clear()
        await fetch_eightfold(c, NF, "Netflix", list_only=True)  # remembered: no PCSX attempt this time
    assert calls == ["/robots.txt", "/api/apply/v2/jobs"]


async def test_fetch_pages_until_count_and_caps_pages(web, monkeypatch):
    async with client_for(web, _site(total=25)) as c:
        assert len(await fetch_eightfold(c, MS, "Microsoft", list_only=True)) == 10  # probe: one page
        jobs = await fetch_eightfold(c, MS, "Microsoft")
        assert len(jobs) == 25 and len({j.external_id for j in jobs}) == 25
    monkeypatch.setattr(ef, "MAX_PAGES", 2)
    monkeypatch.setattr(ef, "MAX_DETAILS", 5)
    calls: list = []
    async with client_for(web, _site(total=25, calls=calls)) as c:
        jobs = await fetch_eightfold(c, MS, "Microsoft")
    assert len(jobs) == 20 and calls.count("/api/pcsx/search") == 2
    assert calls.count("/api/pcsx/position_details") == 5
    assert sum(1 for j in jobs if j.description_text) == 5  # the rest keep listing data only


async def test_fetch_list_only_makes_no_detail_calls(web):
    calls: list = []
    async with client_for(web, _site(pcsx=False, calls=calls)) as c:
        jobs = await fetch_eightfold(c, NF, "Netflix", list_only=True)
    assert len(jobs) == 3 and all(j.description_text == "" for j in jobs)
    assert not any(p.startswith("/api/apply/v2/jobs/") for p in calls)


async def test_fetch_keeps_listing_when_details_fail_but_raises_if_all_fail(web):
    async with client_for(web, _site(fail_details=True)) as c:
        with pytest.raises(RuntimeError, match="detail requests failed"):
            await fetch_eightfold(c, MS, "Microsoft")


async def test_rate_limit_retried_with_the_cookie_it_set(web):
    async with client_for(web, _site(rate_limit=True)) as c:
        jobs = await fetch_eightfold(c, MS, "Microsoft", list_only=True)
    assert len(jobs) == 3


async def test_http_errors_propagate(web):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(404, json={"message": "not found"})

    async with client_for(web, handler) as c:
        with pytest.raises(httpx.HTTPStatusError) as e:
            await fetch_eightfold(c, MS, "Microsoft")
    assert e.value.response.status_code == 404


async def test_robots_disallow_blocks_api_calls(web):
    calls: list = []
    async with client_for(web, _site(robots="User-agent: *\nDisallow: /api/\n", calls=calls)) as c:
        with pytest.raises(FetchError, match="robots"):
            await fetch_eightfold(c, MS, "Microsoft")
    assert calls == ["/robots.txt"]


async def test_private_host_refused_before_any_request(web):
    calls: list = []
    async with client_for(web, _site(calls=calls)) as c:
        with pytest.raises(FetchError):
            await fetch_eightfold(c, "jobs.internal.acme.com__acme.com", "Acme")
    assert calls == []


@pytest.mark.parametrize("slug", ["localhost__acme.com", *[f"x.com__{s}" for s in SQLI]])
async def test_malformed_slug_never_fetches(web, slug):
    calls: list = []
    async with client_for(web, _site(calls=calls)) as c:
        with pytest.raises(ValueError):
            await fetch_eightfold(c, slug, "Acme")
    assert calls == []


async def test_works_through_discoverys_polite_client(web):
    async with client_for(web, _site(pcsx=False)) as c:
        jobs = await PROBE_FETCHERS["eightfold"](_PoliteClient(PoliteFetcher(c)), NF, NF)
    assert len(jobs) == 3


# ── robots.txt precedence ─────────────────────────────────────────────────
@pytest.mark.parametrize("path,ok", [
    ("/api/pcsx/search?domain=microsoft.com&start=0", True),
    ("/api/pcsx/position_details?position_id=1&domain=microsoft.com", True),
    ("/api/apply/v2/jobs?domain=netflix.com&start=0&num=10", True),
    ("/api/apply/v2/jobs/790318907811?domain=netflix.com", True),
    ("/careers/job/790318907811", True),
    ("/api/other", False),
    ("/static/js/app.js", False),
])
async def test_real_eightfold_robots_allows_only_the_api_and_careers(web, path, ok):
    """RFC 9309: the longest matching rule wins, so "Allow: /api/pcsx" beats the earlier "Disallow: /"."""
    parser = RobotFileParser()
    parser.parse(ROBOTS.splitlines())
    assert can_fetch(parser, ROBOTS_TOKEN, f"https://explore.jobs.netflix.net{path}") is ok
    async with client_for(web, _site()) as c:
        assert await PoliteFetcher(c).allowed(f"https://explore.jobs.netflix.net{path}") is ok


def test_robots_precedence_basics():
    def allowed(robots, path, agent=ROBOTS_TOKEN):
        p = RobotFileParser()
        p.parse(robots.splitlines())
        return can_fetch(p, agent, f"https://x.com{path}")

    assert allowed("User-agent: *\nAllow: /a\nDisallow: /a/b\n", "/a/b/c") is False  # longer rule wins
    assert allowed("User-agent: *\nDisallow: /a\nAllow: /a\n", "/a") is True  # tie: allow
    assert allowed("User-agent: *\nDisallow:\n", "/x") is True
    assert allowed(f"User-agent: {ROBOTS_TOKEN}\nDisallow: /\n\nUser-agent: *\nAllow: /\n", "/x") is False
    assert allowed("User-agent: other\nDisallow: /\n", "/x") is True


def test_company_name_from_slug():
    from jobfinder.services.resolver import name_from_slug

    assert name_from_slug("eightfold", MS) == "Microsoft"
    assert name_from_slug("eightfold", NF) == "Netflix"


def test_review_edge_cases():
    # an unrelated page that merely links to eightfold.ai is not an Eightfold board
    page = '<a href="https://eightfold.ai/blog">blog</a><a href="/x?domain=acme.com">x</a>'
    assert detect_html("https://jobs.acme.com/careers", page) is None
    # the combined slug must fit validate.SLUG_RE (200 chars)
    long_host = ".".join(["a" * 60] * 3) + ".com"
    assert ef.make_slug(long_host, "b" * 60 + ".com") is None
    jobs = parse_eightfold([{"id": 1, "name": "Engineer", "work_location_option": "on_site"},
                            {"id": 2, "name": "Engineer", "workLocationOption": "remote_global"}],
                           "Acme", "jobs.acme.com__acme.com")
    assert [j.workplace_type for j in jobs] == ["onsite", "remote"]


async def test_a_403_that_is_not_pcsx_disabled_propagates(web):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(403, text="Forbidden")

    async with client_for(web, handler) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_eightfold(c, MS, "Microsoft")


async def test_non_numeric_count_does_not_break_paging(web):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        positions = [] if int(req.url.params["start"]) else PCSX_LIST["data"]["positions"]
        return httpx.Response(200, json={"data": {"count": "2,400", "positions": positions}})

    async with client_for(web, handler) as c:
        assert len(await fetch_eightfold(c, MS, "Microsoft", list_only=True)) == 3
