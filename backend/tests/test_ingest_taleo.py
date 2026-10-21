import json
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import httpx
import pytest
from security_payloads import PRIVATE_URLS, SCRIPT_URLS, SQLI, XSS_TEXT

from jobfinder.ingest import http_safety
from jobfinder.integrations.ats import SLUG_RE, taleo
from jobfinder.integrations.ats.registry import ATS_FETCHERS, PROBE_FETCHERS
from jobfinder.integrations.ats.taleo import (
    CareerSectionUnavailable,
    extract_portal,
    fetch_taleo,
    job_description,
    parse_date,
    parse_slug,
    parse_taleo,
)
from jobfinder.integrations.directories import board_from_url
from jobfinder.services.board_validation import validate_board
from tests.fakes import fake_resolve_public, handle

FIX = Path(__file__).parent / "fixtures"
SLUG = "cu__2"
HOST = "cu.taleo.net"
LIST = json.loads((FIX / "taleo_list.json").read_text())
SEARCH_HTML = (FIX / "taleo_search.html").read_text()
JOB_HTML = (FIX / "taleo_job.html").read_text()
DEAD_HTML = (FIX / "taleo_unavailable.html").read_text()


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """robots.txt is fetched through safe_fetch (IP-pinned): no real DNS in unit tests."""
    monkeypatch.setattr(http_safety, "resolve_public", fake_resolve_public())


def req(job_id, contest, title="Engineer", loc='["Aurora"]', date="Oct 13, 2026"):
    return {"jobId": job_id, "contestNo": contest, "column": [title, loc, date], "linkedColumn": 0,
            "locationsColumns": [1]}


def listing(reqs, total=None, page=1, size=25):
    return {"requisitionList": reqs, "careerSectionUnAvailable": False,
            "pagingData": {"currentPageNo": page, "pageSize": size, "totalCount": len(reqs) if total is None else total}}


def tenant(web, pages=None, *, search=SEARCH_HTML, job=JOB_HTML, robots=None, job_status=200, list_status=200,
              calls=None):
    """Routes like the real tenant: search page, robots.txt, the searchjobs API (paged) and job pages."""
    pages = pages if pages is not None else [LIST]

    def handler(request: httpx.Request) -> httpx.Response:
        url, path = request.url, request.url.path
        if calls is not None:
            calls.append(request)
        if path == "/robots.txt":
            return httpx.Response(200, text=robots) if robots else httpx.Response(404)
        if path.endswith("/jobsearch.ftl"):
            return httpx.Response(200, text=search, headers={"content-type": "text/html"})
        if path == "/careersection/rest/jobboard/searchjobs":
            assert request.headers["tz"] and url.params["portal"] == "101430233"
            if list_status != 200:
                return httpx.Response(list_status)
            n = json.loads(request.content)["pageNo"]
            return httpx.Response(200, json=pages[n - 1] if n <= len(pages) else listing([], 0))
        if path.endswith("/jobdetail.ftl"):
            return httpx.Response(job_status, text=job, headers={"content-type": "text/html"})
        return httpx.Response(404)

    handle(web, handler)


def client(web, **kw) -> httpx.AsyncClient:
    tenant(web, **kw)
    return httpx.AsyncClient(follow_redirects=True)


def test_registered():
    assert ATS_FETCHERS["taleo"] is fetch_taleo
    assert isinstance(PROBE_FETCHERS["taleo"], partial) and PROBE_FETCHERS["taleo"].keywords == {"list_only": True}


def test_parse_slug():
    assert parse_slug(SLUG) == ("cu", "2")
    assert parse_slug("cu__cu_ext_ucd") == ("cu", "cu_ext_ucd")
    assert parse_slug("fa009__ex") == ("fa009", "ex")
    assert SLUG_RE.match("cu__cu_ext_ucd")


@pytest.mark.parametrize("bad", [
    "", "cu", "cu__", "__2", "cu__2__x", "CU__2", "-cu__2", "cu-__2", "c.u__2", "cu.taleo.net__2",
    "cu__a.b", "cu__a/b", "cu__" + "x" * 65, "evil.com/x__2", "a@b__2",
    *[f"{s}__2" for s in SQLI], *[f"cu__{s}" for s in SQLI],
])
def test_malformed_slugs_rejected(bad):
    with pytest.raises(ValueError):
        parse_slug(bad)


@pytest.mark.parametrize("url,expected", [
    ("https://cu.taleo.net/careersection/2/jobdetail.ftl?job=41404&lang=en", ("taleo", "cu__2")),
    ("https://cu.taleo.net/careersection/cu_ext_ucd/jobsearch.ftl?lang=en", ("taleo", "cu__cu_ext_ucd")),
    ("https://fa009.taleo.net/careersection/ex/jobsearch.ftl", ("taleo", "fa009__ex")),
    ("https://CU.Taleo.NET/careersection/2/moresearch.ftl?lang=en", ("taleo", "cu__2")),
    ("http://hdr.taleo.net/careersection/ex/jobdetail.ftl?job=1", ("taleo", "hdr__ex")),
    ("https://cu.taleo.net/careersection/rest/jobboard/searchjobs?portal=1", None),
    ("https://cu.taleo.net/careersection/theme/1001/x.css", None),
    ("https://cu.taleo.net/careersection/2026PRD.1.4.14.3.0/css/x.css", None),
    ("https://cu.taleo.net/", None),
    ("https://cu.taleo.net/careersection/iam/accessmanagement/login.jsf?lang=en", None),
    ("https://cu.taleo.net/careersection/", None),
    ("https://a.b.taleo.net/careersection/2/jobsearch.ftl", None),
    ("https://taleo.net/careersection/2/jobsearch.ftl", None),
    ("https://cu.taleo.net.evil.com/careersection/2/jobsearch.ftl", None),
    ("ftp://cu.taleo.net/careersection/2/jobsearch.ftl", None),
])
def test_board_from_url(url, expected):
    assert board_from_url(url) == expected


@pytest.mark.parametrize("payload", SCRIPT_URLS + SQLI + PRIVATE_URLS)
def test_hostile_urls_never_become_boards(payload):
    assert board_from_url(payload) is None
    assert board_from_url(f"https://cu.taleo.net/careersection/{payload}/jobsearch.ftl") is None
    assert board_from_url(f"https://{payload}.taleo.net/careersection/2/jobsearch.ftl") is None


def test_extract_portal():
    assert extract_portal(SEARCH_HTML) == "101430233"
    assert extract_portal("var x = {portalNo: '101430233', other: 1}") == "101430233"
    assert extract_portal('{"portal":424242}') == "424242"
    assert extract_portal('<input name="portal" id="portal" value="" />') is None
    assert extract_portal(DEAD_HTML) is None
    links = ('<a href="https://t.taleo.net/careersection/int/jobsearch.ftl?lang=en&amp;portal=111">x</a>'
             '<a href="https://t.taleo.net/careersection/ext/jobsearch.ftl?lang=en&amp;portal=222">y</a>')
    assert extract_portal(links, "ext") == "222"  # the link to this section, not the first one
    assert extract_portal(links, "other") == "111"
    assert extract_portal(links + "<script>portalNo: '333'</script>", "ext") == "333"  # own config wins


@pytest.mark.parametrize("text,expected", [
    ("Oct 13, 2026", datetime(2026, 10, 13, tzinfo=UTC)),
    ("October 3, 2026", datetime(2026, 10, 3, tzinfo=UTC)),
    ("Oct 13, 2026, 4:59:05 PM", datetime(2026, 10, 13, 16, 59, 5, tzinfo=UTC)),
    ("13-Oct-2026", datetime(2026, 10, 13, tzinfo=UTC)),
    ("10/13/2026", None), ("03/10/2026", None),  # month/day order is ambiguous across tenants
    ("2026-10-13", datetime(2026, 10, 13, tzinfo=UTC)),
    ("", None), ("Aurora", None), (None, None), ("[\"Aurora\"]", None),
])
def test_parse_date(text, expected):
    assert parse_date(text) == expected


def test_parse_listing():
    jobs = parse_taleo(LIST["requisitionList"], "University of Colorado", SLUG)
    assert len(jobs) == 3
    j = jobs[0]
    assert j.source == "taleo"
    assert j.external_id == "cu:41404"
    assert j.title == "Open Rank Clinical Research Coordinator (Entry – Senior)"
    assert j.url == "https://cu.taleo.net/careersection/2/jobdetail.ftl?job=41404&lang=en"
    assert j.location == "Aurora"
    assert j.posted_at == datetime(2026, 10, 13, tzinfo=UTC)
    assert j.description_text == ""
    assert j.company_name == "University of Colorado"


def test_parse_columns_without_hints_and_multiple_locations():
    r = {"jobId": "9", "contestNo": "", "column": ["Data Analyst", "13-Oct-2026", '["Denver", "Boulder"]']}
    [j] = parse_taleo([r], "X", SLUG)
    assert j.external_id == "cu:9"  # no contestNo: the internal id
    assert j.location == "Denver; Boulder"
    assert j.posted_at == datetime(2026, 10, 13, tzinfo=UTC)


def test_parse_skips_untitled_idless_and_unsafe_ids():
    rows = [
        req("1", "11", title=""), {"jobId": "", "contestNo": "", "column": ["T"]}, "junk", {"column": []},
        *[req(s, s) for s in SQLI],
        req("2", SQLI[0]),  # unsafe contestNo falls back to the safe jobId
    ]
    jobs = parse_taleo(rows, "X", SLUG)
    assert [j.external_id for j in jobs] == ["cu:2"]
    assert jobs[0].url.endswith("job=2&lang=en")


@pytest.mark.parametrize("text", XSS_TEXT)
def test_xss_stays_text(text):
    [j] = parse_taleo([{**req("1", "1", title=f"Engineer {text}"), "description": text}], "X", SLUG)
    assert j.url == "https://cu.taleo.net/careersection/2/jobdetail.ftl?job=1&lang=en"
    assert j.title == f"Engineer {text}"  # kept as plain text; the frontend renders it escaped


def test_job_description():
    d = job_description(JOB_HTML)
    assert d.startswith("University of Colorado Anschutz Medical Campus")
    assert "Job Summary" in d and "%3C" not in d and "<p" not in d
    assert d.count("Job Summary:") == 1  # each rich field appears twice in the page; kept once
    assert job_description("<html>nothing</html>") == ""
    assert job_description(DEAD_HTML) == ""


def test_job_description_js_escapes():
    page = ("api.fillList('requisitionDescriptionInterface', 'descRequisition', ['1', null, "
            "'!*!%3Cp%3EIt\\'s great\\u0021%3C/p%3E', 'It\\'s', '!*!%3Cb%3EMore%3C/b%3E']);")
    assert job_description(page).split() == ["It's", "great!", "More"]


async def test_fetch_with_details(web):
    calls: list[httpx.Request] = []
    async with client(web, calls=calls) as c:
        jobs = await fetch_taleo(c, SLUG, "CU")
    assert len(jobs) == 3
    assert all(j.description_text.startswith("University of Colorado") for j in jobs)
    paths = [r.url.path for r in calls]
    assert paths[0] == "/robots.txt" and "/careersection/2/jobsearch.ftl" in paths
    assert sum(p.endswith("/jobdetail.ftl") for p in paths) == 3
    post = next(r for r in calls if r.method == "POST")
    assert post.headers["content-type"] == "application/json" and post.headers["accept"] == "application/json"
    assert all(r.headers["host"] == HOST for r in calls)
    # Taleo's redirects mislabel plain bodies as gzip, so HTML pages are requested uncompressed
    assert all(r.headers["accept-encoding"] == "identity" for r in calls if r.url.path.endswith(".ftl"))


async def test_fetch_list_only_makes_no_page_calls(web):
    calls: list[httpx.Request] = []
    pages = [listing([req(str(i), str(i)) for i in range(3)], total=6, size=3),
             listing([req(str(i), str(i)) for i in range(3, 6)], total=6, size=3)]
    async with client(web, pages=pages, calls=calls) as c:
        jobs = await fetch_taleo(c, SLUG, "CU", list_only=True)
    assert len(jobs) == 3
    assert not any(r.url.path.endswith("/jobdetail.ftl") for r in calls)
    assert sum(r.method == "POST" for r in calls) == 1


async def test_fetch_pages_until_total(web):
    calls: list[httpx.Request] = []
    pages = [listing([req(str(i), str(i)) for i in range(p * 3, p * 3 + 3)], total=7, size=3) for p in range(2)]
    pages.append(listing([req("6", "6")], total=7, size=3))
    pages.append(listing([req("99", "99")], total=7, size=3))  # never asked for
    async with client(web, pages=pages, calls=calls) as c:
        jobs = await fetch_taleo(c, SLUG, "CU", list_only=False)
    assert sorted(int(j.external_id.split(":")[1]) for j in jobs) == list(range(7))
    assert [json.loads(r.content)["pageNo"] for r in calls if r.method == "POST"] == [1, 2, 3]


async def test_fetch_stops_when_server_repeats_pages(web):
    same = listing([req("1", "1"), req("2", "2")], total=500, size=2)
    async with client(web, pages=[same] * 10) as c:
        jobs = await fetch_taleo(c, SLUG, "CU", list_only=False)
    assert len(jobs) == 2


async def test_detail_cap(monkeypatch, web):
    monkeypatch.setattr(taleo, "MAX_DETAILS", 2)
    calls: list[httpx.Request] = []
    async with client(web, calls=calls) as c:
        jobs = await fetch_taleo(c, SLUG, "CU")
    assert len(jobs) == 3 and sum(bool(j.description_text) for j in jobs) == 2
    assert sum(r.url.path.endswith("/jobdetail.ftl") for r in calls) == 2


async def test_detail_failures_keep_listing_and_all_failing_raises(web):
    async with client(web, job_status=500) as c:
        with pytest.raises(RuntimeError, match="job page requests failed"):
            await fetch_taleo(c, SLUG, "CU")
    one = listing([req("1", "1")])
    async with client(web, pages=[one], job_status=500) as c:
        [j] = await fetch_taleo(c, SLUG, "CU")  # too few to call it a total failure
    assert j.description_text == ""


async def test_robots_blocking_job_pages_keeps_listing(web):
    calls: list[httpx.Request] = []
    robots = "User-agent: *\nDisallow: /careersection/2/jobdetail.ftl\n"
    async with client(web, robots=robots, calls=calls) as c:
        jobs = await fetch_taleo(c, SLUG, "CU")
    assert len(jobs) == 3 and not any(r.url.path.endswith("/jobdetail.ftl") for r in calls)


async def test_robots_blocking_search_page_refuses(web):
    async with client(web, robots="User-agent: *\nDisallow: /\n") as c:
        with pytest.raises(Exception, match="robots"):
            await fetch_taleo(c, SLUG, "CU")


async def test_unavailable_section_is_refused(web):
    async with client(web, search=DEAD_HTML) as c:
        with pytest.raises(CareerSectionUnavailable):
            await fetch_taleo(c, "capps__ex", "Capps", list_only=True)
        result = await validate_board("taleo", "capps__ex", c, list_only=True)
    assert not result.ok and "unavailable" in result.reason


async def test_api_saying_unavailable_is_refused(web):
    dead = {"requisitionList": None, "pagingData": None, "careerSectionUnAvailable": True}
    async with client(web, pages=[dead]) as c:
        with pytest.raises(CareerSectionUnavailable):
            await fetch_taleo(c, SLUG, "CU")


async def test_missing_portal_is_refused(web):
    async with client(web, search="<html><title>Job Search</title></html>") as c:
        with pytest.raises(CareerSectionUnavailable, match="portal"):
            await fetch_taleo(c, SLUG, "CU")


@pytest.mark.parametrize("status", [404, 500])
async def test_listing_http_error_propagates(status, web):
    async with client(web, list_status=status) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_taleo(c, SLUG, "CU")


async def test_empty_listing_body_raises(web):
    def handler(request):
        if request.url.path.endswith("/jobsearch.ftl"):
            return httpx.Response(200, text=SEARCH_HTML)
        if request.method == "POST":
            return httpx.Response(200, content=b"")  # what Taleo does without a tz header
        return httpx.Response(404)

    handle(web, handler)
    async with httpx.AsyncClient() as c:
        with pytest.raises(ValueError):
            await fetch_taleo(c, SLUG, "CU")


async def test_redirect_off_tenant_is_refused(web):
    def handler(request):
        if request.url.host == HOST and request.url.path.endswith("/jobsearch.ftl"):
            return httpx.Response(302, headers={"location": "https://evil.example/careersection/x"})
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=SEARCH_HTML)

    calls: list[httpx.Request] = []

    def logged(request):
        calls.append(request)
        return handler(request)

    handle(web, logged)
    async with httpx.AsyncClient(follow_redirects=True) as c:
        with pytest.raises(CareerSectionUnavailable, match="redirects"):
            await fetch_taleo(c, SLUG, "CU")
    assert all(r.headers["host"] == HOST for r in calls)  # the off-host target is never contacted


async def test_redirect_within_tenant_is_followed(web):
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        path = request.url.path
        if path == "/careersection/cu_ext_ucd/jobsearch.ftl":  # CU sends every section to 2/moresearch.ftl
            return httpx.Response(302, headers={"location": "/careersection/2/moresearch.ftl?lang=en"})
        if path == "/careersection/2/moresearch.ftl":
            return httpx.Response(200, text=SEARCH_HTML)
        if path.endswith("/searchjobs"):
            return httpx.Response(200, json=LIST)
        return httpx.Response(404)

    handle(web, handler)
    async with httpx.AsyncClient() as c:
        jobs = await fetch_taleo(c, "cu__cu_ext_ucd", "CU", list_only=True)
    assert len(jobs) == 3 and jobs[0].url.startswith("https://cu.taleo.net/careersection/cu_ext_ucd/jobdetail")
    assert "/careersection/2/moresearch.ftl" in [r.url.path for r in calls]


async def test_later_page_transport_error_keeps_earlier_pages(web):
    first = listing([req("1", "1"), req("2", "2")], total=10, size=2)

    def handler(request):
        if request.url.path.endswith("/jobsearch.ftl"):
            return httpx.Response(200, text=SEARCH_HTML)
        if request.url.path.endswith("/searchjobs"):
            if json.loads(request.content)["pageNo"] == 1:
                return httpx.Response(200, json=first)
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(404)

    handle(web, handler)
    async with httpx.AsyncClient() as c:
        jobs = await fetch_taleo(c, SLUG, "CU", list_only=True)
        jobs_all = await fetch_taleo(c, SLUG, "CU")  # job pages 404: listing data kept
    assert len(jobs) == 2 and len(jobs_all) == 2


async def test_odd_paging_values_and_null_location(web):
    page = {"requisitionList": [req("1", "1", loc="null"), req("2", "2", loc="[null, 5]")],
            "pagingData": {"pageSize": "2", "totalCount": "2"}}
    async with client(web, pages=[page]) as c:
        jobs = await fetch_taleo(c, SLUG, "CU", list_only=True)
    assert [j.location for j in jobs] == ["", ""]


async def test_invalid_slug_raises_before_network(web):
    calls: list[httpx.Request] = []
    async with client(web, calls=calls) as c:
        for bad in [SQLI[0], "cu", "127.0.0.1__2", "cu__../../x"]:
            with pytest.raises(ValueError):
                await fetch_taleo(c, bad, "X")
    assert calls == []
