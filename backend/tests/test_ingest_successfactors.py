from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from security_payloads import SCRIPT_URLS, SQLI, XSS_TEXT

from jobfinder.ingest import http_safety
from jobfinder.ingest.http_safety import FetchError
from jobfinder.ingest.sources import ATS_FETCHERS, PROBE_FETCHERS
from jobfinder.ingest.sources import successfactors as sf
from jobfinder.ingest.sources.detect import HTML_DETECTORS, detect_board
from jobfinder.ingest.sources.successfactors import (
    detect_successfactors,
    fetch_successfactors,
    make_slug,
    parse_date,
    parse_detail,
    parse_search,
    parse_slug,
    parse_successfactors,
    parse_total,
)
from jobfinder.ingest.validate import SLUG_RE

FIX = Path(__file__).parent / "fixtures"
SEARCH = (FIX / "successfactors_ey_search.html").read_text()
DETAIL = (FIX / "successfactors_ey_detail.html").read_text()
HOST, SLUG, BASE = "careers.ey.com", "careers.ey.com__ey", "https://careers.ey.com/ey"
PUBLIC_IP = "93.184.216.34"
ROBOTS = "User-agent: *\nDisallow: /services/\nDisallow: /applybutton/\n"


@pytest.fixture
def public_dns(monkeypatch):
    async def fake(url):
        return httpx.URL(url).host, PUBLIC_IP

    monkeypatch.setattr(http_safety, "resolve_public", fake)


def _row(jid: str, title: str, href: str | None = None, location: str = "Zurich, CH", date: str = "") -> str:
    href = href if href is not None else f"/ey/job/Zurich-{jid}/{jid}/"
    date_td = f'<td class="colDate"><span class="jobDate">{date}</span></td>' if date else ""
    return (f'<tr class="data-row"><td class="colTitle"><span class="jobTitle hidden-phone">'
            f'<a href="{href}" class="jobTitle-link">{title}</a></span></td>'
            f'<td class="colLocation"><span class="jobLocation">{location}</span></td>{date_td}</tr>')


def _page(rows: list[str], first: int = 1, total: int | None = None) -> str:
    label = (f'<span class="paginationLabel">Results <b>{first} – {first + len(rows) - 1}</b> of <b>{total}</b></span>'
             if total is not None else "")
    return ('<html><head><link href="//rmkcdn.successfactors.com/x.css"/></head><body><table><tbody>'
            + "".join(rows) + f"</tbody></table>{label}</body></html>")


def _site(total: int, page_size: int = 25, detail_status: int = 200, calls: list | None = None, robots: str = ROBOTS):
    """Mock RMK site with `total` jobs (ids 1000..), routed by Host header (safe_fetch connects by IP)."""

    def handler(req: httpx.Request) -> httpx.Response:
        host = req.headers["host"]
        assert host == HOST, host
        assert req.url.host == PUBLIC_IP  # every request, robots.txt included, goes to the validated IP
        path = req.url.path
        if calls is not None:
            calls.append(path if path != "/ey/search/" else f"search@{req.url.params.get('startrow')}")
        if path == "/robots.txt":
            return httpx.Response(200, text=robots)
        assert not path.startswith("/services/")
        if path == "/ey/search/":
            q = parse_qs(req.url.query.decode())
            assert q["sortColumn"] == ["referencedate"] and q["sortDirection"] == ["desc"]
            start = int(q["startrow"][0])
            ids = range(1000 + start, 1000 + min(start + page_size, total))
            return httpx.Response(200, html=_page([_row(str(i), f"Job {i}") for i in ids], start + 1, total))
        if "/ey/job/" in path:
            if detail_status != 200:
                return httpx.Response(detail_status)
            jid = path.rstrip("/").rsplit("/", 1)[-1]
            return httpx.Response(200, html=DETAIL.replace("Manager HR transformatie", f"Detail {jid}"))
        return httpx.Response(404)

    return handler


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── registry / slugs ──────────────────────────────────────────────────────
def test_registered():
    assert ATS_FETCHERS["successfactors"] is fetch_successfactors
    probe = PROBE_FETCHERS["successfactors"]
    assert isinstance(probe, partial) and probe.keywords == {"list_only": True}
    assert detect_successfactors in HTML_DETECTORS


def test_parse_slug():
    assert parse_slug("careers.ey.com__ey") == ("careers.ey.com", "ey")
    assert parse_slug("jobs.Example.co.uk") == ("jobs.example.co.uk", None)
    assert parse_slug("careers.acme.com__Acme_Brand-2") == ("careers.acme.com", "Acme_Brand-2")
    assert make_slug("Careers.EY.com", "ey") == SLUG and SLUG_RE.match(SLUG)


@pytest.mark.parametrize("bad", [
    "", "careers", "careers.ey.com__", "careers.ey.com__ey/../x", "careers.ey.com/ey", "-bad.com",
    "careers.ey.com__services", "careers.ey.com__job", "10.0.0.1", "127.0.0.1__ey", "host_name.com",
    "careers.ey.com__" + "a" * 41, "careers.ey.com:8080", "user@careers.ey.com", "careers..ey.com",
    *SQLI, *SCRIPT_URLS,
])
def test_parse_slug_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_slug(bad)


@pytest.mark.parametrize("bad", ["careers.ey.com__ey;DROP", "localhost", *SQLI])
async def test_fetch_refuses_malformed_slug_without_requests(bad):
    def handler(req):
        raise AssertionError("no request may be made for a malformed slug")

    async with _client(handler) as c:
        with pytest.raises(ValueError):
            await fetch_successfactors(c, bad, "X")


async def test_fetch_refuses_private_host():
    # careers.localhost-free names are still resolved: a name pointing inside is refused before any request
    async with _client(lambda r: httpx.Response(200, text="secret")) as c:
        with pytest.raises(FetchError):
            await fetch_successfactors(c, "localhost.localdomain", "X")


# ── parsing ───────────────────────────────────────────────────────────────
def test_parse_search_fixture():
    rows = parse_search(SEARCH, HOST, BASE)
    assert [r["id"] for r in rows] == ["1406325133", "1396108533", "1251072201"]  # phone-view duplicates dropped
    a = rows[0]
    assert a["title"] == "Manager HR transformatie & change (People consulting)"
    assert a["url"].startswith("https://careers.ey.com/ey/job/Eindhoven-Manager-HR") and a["url"].endswith("/1406325133/")
    assert a["location"] == "Eindhoven, NL, 5613 AM" and a["posted"] == ""
    assert parse_total(SEARCH) == 8814


def test_parse_total_variants():
    assert parse_total(_page([_row("1", "A")], total=1234)) == 1234
    assert parse_total('<span class="paginationLabel">Ergebnisse <b>1 – 25</b> von <b>1.234</b></span>') == 1234
    assert parse_total("<html>no label</html>") is None


@pytest.mark.parametrize("href", [*SCRIPT_URLS, "https://evil.example/ey/job/x/123/", "/ey/about/", "/ey/job/x/abc/"])
def test_parse_search_drops_unsafe_or_foreign_links(href):
    rows = parse_search(_page([_row("1", "Bad", href=href), _row("2", "Good")]), HOST, BASE)
    assert [r["id"] for r in rows] == ["2"]


def test_parse_search_date_column_and_untitled():
    page = _page([_row("1", "Dated", date="Oct 13, 2026"), _row("2", "  "), _row("3", "Euro", date="13 Oct 2026")])
    rows = parse_search(page, HOST, BASE)
    assert [r["id"] for r in rows] == ["1", "3"]
    jobs = parse_successfactors(rows, "EY", SLUG)
    assert [j.posted_at for j in jobs] == [datetime(2026, 10, 13, tzinfo=UTC)] * 2


def test_parse_date():
    assert parse_date("Tue Oct 13 02:00:00 UTC 2026") == datetime(2026, 10, 13, 2, tzinfo=UTC)
    assert parse_date("Mon Sep 1 00:00:00 GMT 2026") == datetime(2026, 9, 1, tzinfo=UTC)
    assert parse_date("2026-10-01") == datetime(2026, 10, 1, tzinfo=UTC)
    assert parse_date("") is None and parse_date(None) is None and parse_date("soon") is None


def test_parse_detail_and_posting():
    d = parse_detail(DETAIL)
    assert d["title"] == "Manager HR transformatie & change (People consulting)"
    assert d["location"] == "Eindhoven, NL, 5613 AM" and d["posted"] == "Tue Oct 13 02:00:00 UTC 2026"
    rows = parse_search(SEARCH, HOST, BASE)
    rows[0]["detail"] = d
    a, b, _ = parse_successfactors(rows, "EY", SLUG)
    assert a.source == "successfactors" and a.external_id == "careers.ey.com:1406325133"
    assert a.posted_at == datetime(2026, 10, 13, 2, tzinfo=UTC) and a.seniority == "manager"
    assert a.description_text.startswith("At EY, we’re all in") and "<" not in a.description_text
    assert a.company_name == "EY"
    # listing-only row
    assert b.description_text == "" and b.posted_at is None and b.location == "Zurich, CH, 8005"


def test_parse_detail_skips_meta_description():
    page = ('<html><head><meta itemprop="description" content="Teaser"></head><body>'
            '<span itemprop="description"><p>The real description.</p></span></body></html>')
    assert "The real description." in parse_detail(page)["description"]


def test_parse_detail_hostile_markup_is_inert():
    page = ('<div itemprop="title">Senior Engineer (Remote)</div><div itemprop="description">'
            + "".join(XSS_TEXT) + "<p>Pay: $150,000 - $190,000</p></div>")
    d = parse_detail(page)
    rows = [{"id": "9", "title": "x", "url": f"{BASE}/job/x/9/", "detail": d}]
    (j,) = parse_successfactors(rows, "Acme", SLUG)
    assert "<script" not in j.description_text and "onerror" not in j.description_text
    assert (j.salary_min, j.salary_max, j.workplace_type, j.seniority) == (150000, 190000, "remote", "senior")


# ── detection ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url,expected", [
    ("https://careers.ey.com/ey/job/Eindhoven-Manager/1406325133/", SLUG),
    ("https://careers.ey.com/ey/search/?q=&locationsearch=", SLUG),
    ("https://jobs.acme.com/job/Berlin-Engineer-10115/987654/", "jobs.acme.com"),
    ("https://jobs.acme.com/search/?q=python", "jobs.acme.com"),
    ("https://Careers.EY.com/ey/", SLUG),  # landing page: brand from the search form's action
    ("https://careers.ey.com/ey/search/job/x/1/", SLUG),  # the first marker segment decides
])
def test_detect(url, expected):
    html = '<link href="//rmkcdn.successfactors.com/a.css"/><form action="/ey/search/#x"></form>'
    if "acme" in url:
        html = '<script>j2w.init({"locale": "en_US"});</script>'
    assert detect_board(url, html) == ("successfactors", expected)


def test_detect_absolute_form_action():
    html = '<link href="//rmkcdn.successfactors.com/a.css"/><form action="https://careers.ey.com/ey/search/">'
    assert detect_board("https://careers.ey.com/", html) == ("successfactors", SLUG)


def test_detect_fixture_pages():
    assert detect_board("https://careers.ey.com/ey/search/?q=", SEARCH) == ("successfactors", SLUG)
    assert detect_board("https://careers.ey.com/ey/job/X/1406325133/", DETAIL) == ("successfactors", SLUG)


@pytest.mark.parametrize("url,html", [
    ("https://careers.acme.com/jobs/1", "<html>Greenhouse board</html>"),  # not RMK
    ("https://news.example.com/sap-buys-jobs2web", "<p>SAP acquired Jobs2Web (jobs2web) in 2011</p>"),
    ("javascript:alert(1)", "rmkcdn.successfactors.com"),
    ("https://10.0.0.1/ey/job/x/1/", "rmkcdn.successfactors.com"),
    ("https://careers.ey.com/services/job/x/1/", "rmkcdn.successfactors.com"),  # reserved brand
    ("https://careers.ey.com/a'%20OR%201=1/job/x/1/", "rmkcdn.successfactors.com"),
])
def test_detect_rejects(url, html):
    assert detect_successfactors(url, html) is None


# ── fetching ──────────────────────────────────────────────────────────────
async def test_fetch_pages_and_details(public_dns, monkeypatch):
    monkeypatch.setattr(sf, "MAX_DETAILS", 5)
    calls: list = []
    async with _client(_site(total=60, calls=calls)) as c:
        jobs = await fetch_successfactors(c, SLUG, "EY")
    assert len(jobs) == 60 and len({j.external_id for j in jobs}) == 60
    assert [p for p in calls if p.startswith("search@")] == ["search@0", "search@25", "search@50"]
    assert calls.count("/robots.txt") == 1
    assert sum("/job/" in p for p in calls) == 5  # detail cap
    assert jobs[0].title == "Detail 1000 & change (People consulting)" and jobs[0].posted_at is not None
    assert jobs[5].title == "Job 1005" and jobs[5].description_text == ""


async def test_fetch_stops_at_max_pages(public_dns, monkeypatch):
    monkeypatch.setattr(sf, "MAX_PAGES", 2)
    async with _client(_site(total=1000)) as c:
        jobs = await fetch_successfactors(c, SLUG, "EY", list_only=True)
    assert len(jobs) == 25  # list_only: one page
    monkeypatch.setattr(sf, "MAX_DETAILS", 0)
    async with _client(_site(total=1000)) as c:
        jobs = await fetch_successfactors(c, SLUG, "EY")
    assert len(jobs) == 50


async def test_fetch_without_total_stops_on_repeat(public_dns, monkeypatch):
    """No pagination label: paging ends when a page brings nothing new (sites that ignore startrow)."""
    monkeypatch.setattr(sf, "MAX_DETAILS", 0)
    pages = {"n": 0}

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        pages["n"] += 1
        return httpx.Response(200, html=_page([_row("1", "A"), _row("2", "B")]))

    async with _client(handler) as c:
        jobs = await fetch_successfactors(c, SLUG, "EY")
    assert [j.external_id for j in jobs] == ["careers.ey.com:1", "careers.ey.com:2"] and pages["n"] == 2


async def test_detail_failures_keep_listing(public_dns):
    async with _client(_site(total=2, detail_status=500)) as c:
        jobs = await fetch_successfactors(c, SLUG, "EY")  # < 3 details: no all-failed error
    assert [j.title for j in jobs] == ["Job 1000", "Job 1001"]
    async with _client(_site(total=5, detail_status=500)) as c:
        with pytest.raises(RuntimeError, match="all 5"):
            await fetch_successfactors(c, SLUG, "EY")


async def test_listing_http_error_propagates(public_dns):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(404)

    async with _client(handler) as c:
        with pytest.raises(httpx.HTTPStatusError) as e:
            await fetch_successfactors(c, SLUG, "EY")
    assert e.value.response.status_code == 404


async def test_robots_disallow_is_honoured(public_dns):
    calls: list = []
    async with _client(_site(total=3, calls=calls, robots="User-agent: *\nDisallow: /ey/search/\n")) as c:
        with pytest.raises(FetchError, match="robots"):
            await fetch_successfactors(c, SLUG, "EY")
    assert calls == ["/robots.txt"]


def test_board_name_from_slug():
    from jobfinder.ingest.resolve import name_from_slug

    assert name_from_slug("successfactors", SLUG) == "Ey"
    assert name_from_slug("successfactors", "jobs.acme-robotics.co.uk") == "Acme Robotics"
