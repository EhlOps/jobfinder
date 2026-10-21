from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import httpx
import pytest
from security_payloads import SCRIPT_URLS, SQLI

from jobfinder.integrations import http_safety
from jobfinder.integrations.ats.detect import detect_board
from jobfinder.integrations.ats.icims import (
    NotIcimsBoard,
    detect_icims,
    fetch_icims,
    parse_icims,
    parse_jsonld,
    parse_search_page,
    parse_sitemap,
    parse_slug,
    title_from_path,
)
from jobfinder.integrations.ats.registry import ATS_FETCHERS, PROBE_FETCHERS
from jobfinder.integrations.directories import board_from_url, boards_from_text
from jobfinder.integrations.http_safety import FetchError
from jobfinder.services.board_validation import validate_board
from jobfinder.services.resolver import name_from_slug
from tests.fakes import fake_resolve_public, handle

FIX = Path(__file__).parent / "fixtures"
SLUG = "careers-scsengineers"
HOST = "careers-scsengineers.icims.com"
SITEMAP = (FIX / "icims_sitemap.xml").read_text()
SEARCH_P0 = (FIX / "icims_search_p0.html").read_text()
SEARCH_P1 = (FIX / "icims_search_p1.html").read_text()
JOB = (FIX / "icims_job_3825.html").read_text()
ROBOTS = "User-agent: *\nDisallow: /jobs/login\nDisallow: /connect\n"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(http_safety, "resolve_public", fake_resolve_public())


def portal(web, routes: dict, seen: list | None = None, robots: str = ROBOTS):
    """A client onto a portal mocked on `web`; safe_fetch connects by IP, so route on the Host header. `routes` maps
    "host/path?query" (or "host/path") to a Response or a callable returning one."""
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.headers["host"]
        path = request.url.path
        if seen is not None:
            seen.append(f"{host}{path}" + (f"?{request.url.query.decode()}" if request.url.query else ""))
        if path == "/robots.txt":
            return httpx.Response(200, text=robots)
        query = request.url.query.decode()
        resp = routes.get(f"{host}{path}?{query}", routes.get(f"{host}{path}"))
        resp = resp() if callable(resp) else resp
        return resp if resp is not None else httpx.Response(404, text="not found")

    handle(web, handler)
    return httpx.AsyncClient()


def xml(text):
    return httpx.Response(200, text=text, headers={"content-type": "text/xml;charset=UTF-8"})


def page(text):
    return httpx.Response(200, text=text, headers={"content-type": "text/html;charset=UTF-8"})


# ── registry / slugs / URLs ──────────────────────────────────────────────
def test_registered():
    assert ATS_FETCHERS["icims"] is fetch_icims
    probe = PROBE_FETCHERS["icims"]
    assert isinstance(probe, partial) and probe.func is fetch_icims and probe.keywords == {"list_only": True}


def test_parse_slug():
    assert parse_slug("careers-scsengineers") == HOST
    assert parse_slug("Careers-AMD") == "careers-amd.icims.com"
    for bad in ("", "scsengineers", "www", "careers-", "-careers", "careers_x", "a.b-c", "careers-x/../y",
                "careers-x.evil.com", "careers-x:8080", "c" * 64 + "-x", *SQLI):
        with pytest.raises(ValueError):
            parse_slug(bad)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://careers-scsengineers.icims.com/jobs/3825/project-engineer/job", ("icims", SLUG)),
        ("https://careers-scsengineers.icims.com/jobs/3825/project-engineer/job?in_iframe=1", ("icims", SLUG)),
        ("https://CAREERS-SCSEngineers.icims.com/jobs/search?ss=1", ("icims", SLUG)),
        ("https://uscareers-acme.icims.com/", ("icims", "uscareers-acme")),
        ("http://jobs-acme-corp.icims.com/jobs/1/x/job", ("icims", "jobs-acme-corp")),
        ("https://www.icims.com/", None),
        ("https://login.icims.com/x", None),
        ("https://icims.com/jobs/1/x/job", None),
        ("https://careers-acme.icims.com.evil.example/jobs/1/x/job", None),
        ("https://a.careers-acme.icims.com/", None),
        ("https://careers-acme.icims.co/", None),
        ("javascript:alert(1)//careers-acme.icims.com", None),
    ],
)
def test_board_from_url(url, expected):
    assert board_from_url(url) == expected


def test_boards_from_text():
    assert boards_from_text("apply at https://careers-acme.icims.com/jobs/12/eng/job?in_iframe=1.") == [("icims", "careers-acme")]


def test_name_from_slug():
    assert name_from_slug("icims", "careers-scsengineers") == "Scsengineers"
    assert name_from_slug("icims", "careers-acme-robotics") == "Acme Robotics"


def test_detect_icims():
    iframe = '<iframe src="https://careers-acme.icims.com/jobs/search?ss=1&in_iframe=1"></iframe>'
    assert detect_icims("https://acme.com/careers", iframe) == ("icims", "careers-acme")
    escaped = '<script>var u = "https:\\/\\/careers-acme.icims.com\\/jobs"</script>'
    assert detect_icims("https://acme.com/careers", escaped) == ("icims", "careers-acme")
    assert detect_board("https://acme.com/careers", iframe) == ("icims", "careers-acme")
    # iCIMS's own marketing links ("powered by") aren't a board
    assert detect_icims("https://acme.com", '<a href="https://www.icims.com/">Powered by iCIMS</a>') is None
    assert detect_icims("https://acme.com", "<p>no portal here</p>") is None
    # an asset host earlier in the page doesn't shadow the real portal
    assets = '<script src="https://cdn-static.icims.com/x.js"></script>' + iframe
    assert detect_icims("https://acme.com/careers", assets) == ("icims", "careers-acme")


def test_title_from_path():
    assert title_from_path("senior-hydrogeologist---groundwater") == "Senior Hydrogeologist - Groundwater"
    assert title_from_path("field-technician-i") == "Field Technician I"
    assert title_from_path("") == ""


# ── parsers ──────────────────────────────────────────────────────────────
def test_parse_sitemap_keeps_only_this_portals_jobs():
    entries = parse_sitemap(SITEMAP, HOST)
    assert [e["id"] for e in entries] == ["4080", "4078", "4077", "4010", "3825"]  # deduped, foreign host dropped
    assert entries[0] == {"id": "4080", "path": "administrative-assistant"}
    assert parse_sitemap(SITEMAP, "careers-other.icims.com") == []
    assert parse_sitemap("", HOST) == [] and parse_sitemap("<html>nope</html>", HOST) == []


def test_parse_search_page():
    cards, pages = parse_search_page(SEARCH_P0, HOST)
    assert pages == 2 and [c["id"] for c in cards] == ["4080", "4078"]
    a = cards[0]
    assert a["title"] == "Administrative Assistant" and a["location"] == "US-KS-Overland Park"
    assert a["path"] == "administrative-assistant"
    cards, pages = parse_search_page(SEARCH_P1, HOST)
    assert pages == 2 and [c["title"] for c in cards] == ["Senior Geotechnical Engineer"]
    assert parse_search_page("", HOST) == ([], 0)
    assert parse_search_page("<!-- -->", HOST) == ([], 0)


def test_parse_search_page_xhtml_and_relative_links():
    card = '<li class="iCIMS_JobCardItem"><div class="title"><a href="/jobs/12/eng/job?in_iframe=1"><h3>Eng</h3>' \
           '</a></div></li>'
    xhtml = f'<?xml version="1.0" encoding="UTF-8"?><html><body><ul>{card}</ul>Page 1 of 1</body></html>'
    cards, pages = parse_search_page(xhtml, "careers-acme.icims.com")
    assert pages == 1 and [(c["id"], c["path"], c["title"]) for c in cards] == [("12", "eng", "Eng")]


def test_parse_jsonld_variants():
    ld = parse_jsonld(JOB)
    assert ld["title"] == "Project Engineer" and ld["datePosted"].startswith("2026-06-23")
    graph = '<script type="application/ld+json">{"@graph": [{"@type": "Organization"}, ' \
            '{"@type": ["JobPosting"], "title": "G"}]}</script>'
    assert parse_jsonld(graph)["title"] == "G"
    listed = "<script type='application/ld+json'>[{\"@type\": \"WebPage\"}, {\"@type\": \"JobPosting\", \"title\": \"L\"}]</script>"
    assert parse_jsonld(listed)["title"] == "L"
    broken = '<script type="application/ld+json">{not json</script>' + graph
    assert parse_jsonld(broken)["title"] == "G"
    assert parse_jsonld("<html></html>") is None


def test_parse_with_detail():
    items = [{"id": "3825", "path": "project-engineer", "detail": parse_jsonld(JOB)},
             {"id": "4080", "path": "administrative-assistant", "title": "Administrative Assistant",
              "location": "US-KS-Overland Park"},
             {"id": "4078", "path": "field-technician-i"}]
    a, b, c = parse_icims(items, "SCS Engineers", SLUG)
    assert a.source == "icims" and a.external_id == "careers-scsengineers:3825"
    assert a.url == "https://careers-scsengineers.icims.com/jobs/3825/project-engineer/job"
    assert a.title == "Project Engineer" and a.location == "West Des Moines, IA, US"
    assert (a.salary_min, a.salary_max, a.salary_currency) == (65000, 100000, "USD")
    assert a.posted_at == datetime(2026, 6, 23, 4, tzinfo=UTC) and a.employment_type == "Full time"
    assert "Project Engineer" in a.description_text and "<p>" not in a.description_text
    # listing-only items keep the card data (description empty, so stored details survive the upsert),
    # or fall back to the URL slug
    assert (b.title, b.location, b.description_text, b.posted_at) == (
        "Administrative Assistant", "US-KS-Overland Park", "", None)
    assert c.title == "Field Technician I" and c.seniority is None


def test_parse_skips_and_sanitises():
    hourly = {"@type": "JobPosting", "title": "Tech", "jobLocationType": "TELECOMMUTE",
              "baseSalary": {"currency": "USD", "value": {"minValue": 20, "maxValue": 30, "unitText": "HOUR"}},
              "datePosted": "not a date"}
    items = [{"id": "", "path": "x"}, {"id": "abc", "path": "x"}, {"id": "1", "path": "../../admin"},
             {"id": "2", "path": "a%2F..%2Fb"}, {"id": "3", "path": ""},
             {"id": "4", "path": "tech", "detail": hourly}]
    jobs = parse_icims(items, "Acme", "careers-acme")
    assert [j.external_id for j in jobs] == ["careers-acme:4"]
    assert jobs[0].salary_min is None and jobs[0].workplace_type == "remote" and jobs[0].posted_at is None
    # hourly pay without a unitText isn't mistaken for an annual salary
    no_unit = {**hourly, "baseSalary": {"minValue": 23, "maxValue": 25, "currency": "USD"}}
    (job,) = parse_icims([{"id": "5", "path": "tech", "detail": no_unit}], "Acme", "careers-acme")
    assert job.salary_min is None
    with pytest.raises(ValueError):
        parse_icims(items, "Acme", "evil.com")


@pytest.mark.parametrize("bad", SCRIPT_URLS)
def test_hostile_urls_are_inert(bad):
    # a hostile JSON-LD url / card href never becomes the posting URL, which is always rebuilt on the portal
    ld = {"@type": "JobPosting", "title": "X", "url": bad}
    (job,) = parse_icims([{"id": "7", "path": "x", "detail": ld}], "Acme", "careers-acme")
    assert job.url == "https://careers-acme.icims.com/jobs/7/x/job"
    card = f'<li class="iCIMS_JobCardItem"><div class="title"><a href="{bad}"><h3>X</h3></a></div></li>'
    assert parse_search_page(f"<ul>{card}</ul>", "careers-acme.icims.com")[0] == []
    assert parse_sitemap(f"<urlset><url><loc>{bad}</loc></url></urlset>", "careers-acme.icims.com") == []


# ── fetching ─────────────────────────────────────────────────────────────
def no_ld():
    return page("<html><body>gone</body></html>")


async def test_fetch_search_pages_then_details(web):
    seen = []
    routes = {
        f"{HOST}/jobs/search?ss=1&in_iframe=1&pr=0": page(SEARCH_P0),
        f"{HOST}/jobs/search?ss=1&in_iframe=1&pr=1": page(SEARCH_P1),
        f"{HOST}/jobs/4078/field-technician-i/job?in_iframe=1": page(JOB),
        f"{HOST}/jobs/4077/senior-geotechnical-engineer/job?in_iframe=1": no_ld,
        # 4080's detail page 404s: the posting keeps its card title and location
    }
    async with portal(web, routes, seen) as c:
        listed = await fetch_icims(c, SLUG, "SCS", list_only=True)
        assert [j.title for j in listed] == ["Administrative Assistant", "Field Technician I"]  # first page only
        jobs = await fetch_icims(c, SLUG, "SCS")
    assert [j.external_id for j in jobs] == [f"{SLUG}:4080", f"{SLUG}:4078", f"{SLUG}:4077"]
    a, b, _ = jobs
    assert (a.title, a.location, a.description_text) == ("Administrative Assistant", "US-KS-Overland Park", "")
    assert b.title == "Project Engineer" and b.salary_min == 65000 and b.description_text  # JSON-LD wins
    assert f"{HOST}/jobs/search?ss=1&in_iframe=1&pr=1" in seen and not any("pr=2" in s for s in seen)
    assert not any("sitemap" in s for s in seen) and all(s.startswith(HOST) for s in seen)


async def test_fetch_falls_back_to_sitemap(web):
    seen = []
    routes = {
        f"{HOST}/jobs/search?ss=1&in_iframe=1&pr=0": httpx.Response(403, text="Your IP address is not trusted"),
        f"{HOST}/sitemap.xml": xml(SITEMAP),
        f"{HOST}/jobs/3825/project-engineer/job?in_iframe=1": page(JOB),
        f"{HOST}/jobs/4078/field-technician-i/job?in_iframe=1": no_ld,
        f"{HOST}/jobs/4077/senior-geotechnical-engineer/job?in_iframe=1": no_ld,
        f"{HOST}/jobs/4010/landfill-gas-field-technician/job?in_iframe=1": no_ld,
    }
    async with portal(web, routes, seen) as c:
        jobs = await fetch_icims(c, SLUG, "SCS Engineers")
    by_id = {j.external_id.split(":")[1]: j for j in jobs}
    assert list(by_id) == ["4080", "4078", "4077", "4010", "3825"]
    assert by_id["3825"].salary_min == 65000 and by_id["3825"].description_text
    assert by_id["4080"].title == "Administrative Assistant" and by_id["4080"].description_text == ""


async def test_list_only_uses_sitemap_without_details(web):
    seen = []
    async with portal(web, {f"{HOST}/sitemap.xml": xml(SITEMAP)}, seen) as c:
        jobs = await PROBE_FETCHERS["icims"](c, SLUG, "SCS")
    assert len(jobs) == 5 and jobs[1].title == "Field Technician I"
    assert not any("/job?" in s for s in seen)


async def test_discovery_probe_goes_through_safe_fetch(web):
    # discovery hands fetchers a robots-checking get/post wrapper; the adapter unwraps it and still uses
    # safe_fetch (SSRF checks, byte caps) with that wrapper's robots cache
    from jobfinder.integrations.robots import PoliteFetcher
    from jobfinder.services.discovery import _PoliteClient

    seen = []
    async with portal(web, {f"{HOST}/jobs/search?ss=1&in_iframe=1&pr=0": page(SEARCH_P0)}, seen) as c:
        result = await validate_board("icims", SLUG, _PoliteClient(PoliteFetcher(c)), list_only=True)
    assert result.ok and result.job_count == 2
    assert seen == [f"{HOST}/robots.txt", f"{HOST}/jobs/search?ss=1&in_iframe=1&pr=0"]


async def test_all_details_failing_raises(web):
    async with portal(web, {f"{HOST}/sitemap.xml": xml(SITEMAP)}) as c:
        with pytest.raises(RuntimeError, match="all 5"):
            await fetch_icims(c, SLUG, "SCS")


async def test_missing_board_is_http_status_error(web):
    async with portal(web, {}) as c:
        with pytest.raises(httpx.HTTPStatusError) as e:
            await fetch_icims(c, "careers-nope", "Nope")
    assert e.value.response.status_code == 404


async def test_redirect_off_icims_is_refused(web):
    seen = []
    off = httpx.Response(301, headers={"location": "https://www.gdit.com/careers"})
    routes = {"careers-gdit.icims.com/sitemap.xml": off,
              "careers-gdit.icims.com/jobs/search?ss=1&in_iframe=1&pr=0": off,
              "www.gdit.com/careers": page("<html>our own careers site</html>")}
    async with portal(web, routes, seen) as c:
        with pytest.raises(NotIcimsBoard, match="www.gdit.com"):
            await fetch_icims(c, "careers-gdit", "GDIT")
        result = await validate_board("icims", "careers-gdit", c, list_only=True)
    assert not result.ok and "not an iCIMS board" in result.reason
    assert not any(s.startswith("www.gdit.com") for s in seen)  # refused before following the redirect


async def test_robots_disallow_all_is_respected(web):
    seen = []
    routes = {f"{HOST}/sitemap.xml": xml(SITEMAP), f"{HOST}/jobs/search?ss=1&in_iframe=1&pr=0": page(SEARCH_P0)}
    async with portal(web, routes, seen, robots="User-agent: *\nDisallow: /\n") as c:
        with pytest.raises(FetchError, match="robots"):
            await fetch_icims(c, SLUG, "SCS")
    assert all(s.endswith("/robots.txt") for s in seen)


@pytest.mark.parametrize("bad", [*SQLI, *SCRIPT_URLS, "careers-x.evil.com", "127.0.0.1"])
async def test_malformed_slug_never_fetches(web, bad):
    seen = []
    async with portal(web, {}, seen) as c:
        with pytest.raises(ValueError):
            await fetch_icims(c, bad, "X")
        result = await validate_board("icims", bad, c)
    assert not result.ok and seen == []


async def test_validate_board_counts_postings(web):
    async with portal(web, {f"{HOST}/sitemap.xml": xml(SITEMAP)}) as c:
        result = await validate_board("icims", SLUG, c, list_only=True)
    assert result.ok and result.job_count == 5
