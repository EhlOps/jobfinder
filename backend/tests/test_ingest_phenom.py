import json
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import httpx
import pytest
from security_payloads import PRIVATE_URLS, SCRIPT_URLS, SQLI, XSS_TEXT

from jobfinder.ingest import http_safety
from jobfinder.ingest.http_safety import FetchError
from jobfinder.integrations.ats import SLUG_RE, phenom
from jobfinder.integrations.ats.detect import HTML_DETECTORS, detect_board
from jobfinder.integrations.ats.phenom import (
    detect_phenom,
    fetch_phenom,
    job_posting_ld,
    parse_phenom,
    parse_slug,
)
from jobfinder.integrations.ats.registry import ATS_FETCHERS, PROBE_FETCHERS
from tests.fakes import PUBLIC_IP, fake_resolve_public, handle

FIX = Path(__file__).parent / "fixtures"
HOST = "careers.mastercard.com"
SLUG = "careers.mastercard.com__us__en"
LIST = json.loads((FIX / "phenom_list.json").read_text())
JOB_HTML = (FIX / "phenom_job.html").read_text()

@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(http_safety, "resolve_public", fake_resolve_public(refuse_internal=True))


def test_registered():
    assert ATS_FETCHERS["phenom"] is fetch_phenom
    assert isinstance(PROBE_FETCHERS["phenom"], partial) and PROBE_FETCHERS["phenom"].keywords == {"list_only": True}
    assert detect_phenom in HTML_DETECTORS


def test_parse_slug():
    assert parse_slug(SLUG) == (HOST, "us", "en")
    assert parse_slug("jobs.thermofisher.com__global__en") == ("jobs.thermofisher.com", "global", "en")
    assert SLUG_RE.match(SLUG)


@pytest.mark.parametrize("bad", [
    "", "careers.mastercard.com", "careers.mastercard.com__us", "careers.mastercard.com__us__en__x",
    "localhost__us__en", "127.0.0.1__us__en", "careers.x.com__US1__en", "careers.x.com__us__english1",
    "evil.com/x__us__en", "a@b.com__us__en", "careers.x.com__u__en", "-bad.com__us__en", "Careers.X.COM__us__en",
    *[f"{s}__us__en" for s in SQLI], *[f"careers.x.com__{s}__en" for s in SQLI],
])
def test_malformed_slugs_rejected(bad):
    with pytest.raises(ValueError):
        parse_slug(bad)


@pytest.mark.parametrize("url,expected", [
    ("https://careers.mastercard.com/us/en/job/R-288138/Vice-President", ("phenom", SLUG)),
    ("https://careers.mastercard.com/us/en/search-results", ("phenom", SLUG)),
    ("https://Jobs.ThermoFisher.com/global/en/job/R-1", ("phenom", "jobs.thermofisher.com__global__en")),
    # no locale in the path: fall back to the page's baseUrl
    ("https://careers.mastercard.com/", ("phenom", SLUG)),
    ("https://careers.mastercard.com/job/R-1", ("phenom", SLUG)),
    ("ftp://careers.mastercard.com/us/en/", None),
    ("https://127.0.0.1/us/en/", None),
    ("https://localhost/us/en/", None),
])
def test_detect_from_page(url, expected):
    assert detect_phenom(url, JOB_HTML) == expected
    assert detect_board(url, JOB_HTML) == expected


def test_detect_needs_both_markers():
    assert detect_phenom("https://careers.x.com/us/en/", "<script src='https://cdn.phenompeople.com/a.js'>") is None
    assert detect_phenom("https://careers.x.com/us/en/", '<script>phApp.ddo = {"refNum":"X"}</script>') is None
    html = '<link href="https://cdn.phenompeople.com"><script>var x = {"refNum":"ACME"}</script>'
    assert detect_phenom("https://careers.x.com/us/en/", html) == ("phenom", "careers.x.com__us__en")
    # baseUrl for another host is not trusted; no locale anywhere -> None
    other = '<x src="phenompeople"></x><script>phApp.ddo={"baseUrl":"https://evil.com/us/en/"}</script>'
    assert detect_phenom("https://careers.x.com/", other) is None


@pytest.mark.parametrize("payload", SCRIPT_URLS + SQLI)
def test_detect_hostile_page_urls_inert(payload):
    found = detect_phenom(payload, JOB_HTML)
    assert found is None or SLUG_RE.match(found[1])
    if found:
        parse_slug(found[1])


def test_job_posting_ld():
    ld = job_posting_ld(JOB_HTML)
    assert ld["@type"] == "JobPosting" and ld["title"] == "Vice President - Open Finance Sales"
    assert job_posting_ld("<script type='application/ld+json'>{broken</script>") == {}
    graph = '<script type="application/ld+json">{"@graph":[{"@type":"WebPage"},{"@type":"JobPosting","title":"T"}]}</script>'
    assert job_posting_ld(graph)["title"] == "T"


def test_parse_listing_only():
    jobs = parse_phenom(LIST["refineSearch"]["data"]["jobs"], "Mastercard", SLUG)
    assert [j.external_id for j in jobs] == [f"{HOST}:R-288138", f"{HOST}:R-289569", f"{HOST}:R-290228"]
    a, _, c = jobs
    assert a.source == "phenom" and a.title == "Vice President - Open Finance Sales"
    assert a.url == "https://careers.mastercard.com/us/en/job/R-288138"
    assert a.seniority == "manager" and a.employment_type == "Full time"
    assert a.posted_at == datetime(2026, 8, 21, tzinfo=UTC)
    assert "Purchase, United States of America" in a.location and "Miami" in a.location  # multi-location
    assert a.description_text.startswith("Internal alignment")
    assert c.location == "Arlington, Virginia, United States of America"
    # javascript: applyUrl is never used; url is the constructed Phenom page
    assert c.url == "https://careers.mastercard.com/us/en/job/R-290228" and "javascript" not in c.url
    assert c.workplace_type == "remote" and c.seniority == "senior"
    assert (c.salary_min, c.salary_max) == (150000, 190000)


def test_parse_with_detail():
    item = {**LIST["refineSearch"]["data"]["jobs"][0], "detail": job_posting_ld(JOB_HTML)}
    (j,) = parse_phenom([item], "Mastercard", SLUG)
    assert "Our Purpose" in j.description_text and "<p>" not in j.description_text and "&lt;" not in j.description_text
    assert (j.salary_min, j.salary_max) == (180000, 220000)


def test_parse_skips_untitled_idless_and_unsafe_ids():
    jobs = parse_phenom([{"jobId": "A1", "title": " "}, {"title": "No id"}, {"jobId": "../x", "title": "T"},
                         {"jobId": "a/b", "title": "T"}, "junk", {"reqId": "R-9", "title": "OK"}], "X", SLUG)
    assert [j.external_id for j in jobs] == [f"{HOST}:R-9"]


@pytest.mark.parametrize("text", XSS_TEXT)
def test_xss_in_description_stays_text(text):
    (j,) = parse_phenom([{"jobId": "R-1", "title": "T", "descriptionTeaser": text}], "X", SLUG)
    assert "<script" not in j.description_text and "<img" not in j.description_text and "<svg" not in j.description_text


def _handler(calls=None, total=3, jobs=None, fail_pages=False, list_status=200):
    jobs = LIST["refineSearch"]["data"]["jobs"] if jobs is None else jobs

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["host"] == HOST
        if req.url.path != "/robots.txt":  # robots.txt goes through PoliteFetcher's own client call
            assert req.url.host == PUBLIC_IP  # connected by the validated IP
        if calls is not None:
            calls.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: */apply\nDisallow: */jobcart\n")
        if req.method == "POST" and req.url.path == "/widgets":
            if list_status != 200:
                return httpx.Response(list_status, json={})
            body = json.loads(req.content)
            off, size = body["from"], body["size"]
            page = jobs[off:off + size]
            return httpx.Response(200, json={"refineSearch": {"status": 200, "hits": len(page), "totalHits": total,
                                                              "data": {"jobs": page}}})
        if req.method == "GET" and req.url.path.startswith("/us/en/job/"):
            if fail_pages:
                return httpx.Response(500)
            return httpx.Response(200, html=JOB_HTML)
        return httpx.Response(404)

    return handler


async def _run(web, fn, handler):
    handle(web, handler)
    async with httpx.AsyncClient() as client:
        return await fn(client)


async def test_fetch_with_details(web):
    calls: list = []
    jobs = await _run(web, lambda c: fetch_phenom(c, SLUG, "Mastercard"), _handler(calls))
    assert len(jobs) == 3 and all("Our Purpose" in j.description_text for j in jobs)
    post = next(b for m, p, b in calls if m == "POST")
    assert post["lang"] == "en_us" and post["country"] == "us" and post["from"] == 0 and post["size"] == 100
    assert post["ddoKey"] == "refineSearch" and post["siteType"] == "external"
    pages = sorted(p for m, p, _ in calls if m == "GET" and p != "/robots.txt")
    assert pages == ["/us/en/job/R-288138", "/us/en/job/R-289569", "/us/en/job/R-290228"]
    assert sum(p == "/robots.txt" for _, p, _ in calls) == 1  # robots cached per origin


async def test_fetch_list_only_makes_no_page_calls(web):
    calls: list = []
    jobs = await _run(web, lambda c: fetch_phenom(c, SLUG, "Mastercard", list_only=True), _handler(calls))
    assert len(jobs) == 3 and {m for m, p, _ in calls if p != "/robots.txt"} == {"POST"}
    assert jobs[0].description_text.startswith("Internal alignment")


async def test_fetch_pages_until_total(web, monkeypatch):
    monkeypatch.setattr(phenom, "PAGE", 2)
    many = [{"jobId": f"R-{i}", "title": f"Engineer {i}"} for i in range(5)]
    calls: list = []
    jobs = await _run(web, lambda c: fetch_phenom(c, SLUG, "X", list_only=False),
                      _handler(calls, total=5, jobs=many))
    assert [j.external_id for j in jobs] == [f"{HOST}:R-{i}" for i in range(5)]
    assert [b["from"] for m, _, b in calls if m == "POST"] == [0, 2, 4]


async def test_fetch_stops_when_server_repeats_pages(web, monkeypatch):
    monkeypatch.setattr(phenom, "PAGE", 2)
    same = [{"jobId": "R-1", "title": "A"}, {"jobId": "R-2", "title": "B"}]

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.method == "POST":  # ignores `from`, claims a huge total
            return httpx.Response(200, json={"refineSearch": {"totalHits": 10_000, "data": {"jobs": same}}})
        return httpx.Response(200, html=JOB_HTML)

    jobs = await _run(web, lambda c: fetch_phenom(c, SLUG, "X", list_only=True), handler)
    assert len(jobs) == 2
    calls = {"n": 0}

    def counting(req):
        calls["n"] += req.method == "POST"
        return handler(req)

    await _run(web, lambda c: fetch_phenom(c, SLUG, "X"), counting)
    assert calls["n"] == 2  # second page brought nothing new


async def test_detail_failures_keep_listing_and_all_failing_raises(web):
    with pytest.raises(RuntimeError, match="job page requests failed"):
        await _run(web, lambda c: fetch_phenom(c, SLUG, "Mastercard"), _handler(fail_pages=True))
    jobs = await _run(web, lambda c: fetch_phenom(c, SLUG, "Mastercard"),
                      _handler(fail_pages=True, jobs=LIST["refineSearch"]["data"]["jobs"][:1], total=1))
    assert len(jobs) == 1 and jobs[0].description_text.startswith("Internal alignment")


@pytest.mark.parametrize("status", [404, 500])
async def test_listing_http_error_propagates(web, status):
    with pytest.raises(httpx.HTTPStatusError) as e:
        await _run(web, lambda c: fetch_phenom(c, SLUG, "X"), _handler(list_status=status))
    assert e.value.response.status_code == status


async def test_robots_disallow_is_honoured(web):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /widgets\n")
        raise AssertionError(f"fetched {req.url.path} despite robots.txt")

    with pytest.raises(FetchError, match="robots"):
        await _run(web, lambda c: fetch_phenom(c, SLUG, "X"), handler)


async def test_private_host_refused_before_any_request(web):
    def handler(req):
        raise AssertionError("no request may be sent")

    with pytest.raises(FetchError):
        await _run(web, lambda c: fetch_phenom(c, "careers.internal.acme.com__us__en", "X"), handler)


@pytest.mark.parametrize("url", PRIVATE_URLS)
def test_private_urls_never_become_boards(url):
    found = detect_phenom(url + "us/en/", JOB_HTML)
    assert found is None


async def test_server_capped_page_size_still_pages_through_everything(web):
    many = [{"jobId": f"R-{i}", "title": f"Engineer {i}"} for i in range(25)]
    offsets = []

    def handler(req):
        if req.url.path == "/robots.txt":  # job pages off limits, so only the listing is used
            return httpx.Response(200, text="User-agent: *\nDisallow: /us/en/job/\n")
        off = json.loads(req.content)["from"]
        offsets.append(off)
        return httpx.Response(200, json={"refineSearch": {"totalHits": 25, "data": {"jobs": many[off:off + 10]}}})

    jobs = await _run(web, lambda c: fetch_phenom(c, SLUG, "X"), handler)
    assert len(jobs) == 25 and offsets == [0, 10, 20]


async def test_later_page_failure_keeps_earlier_pages(web, monkeypatch):
    monkeypatch.setattr(phenom, "PAGE", 2)
    many = [{"jobId": f"R-{i}", "title": f"Engineer {i}"} for i in range(6)]

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /us/en/job/\n")
        off = json.loads(req.content)["from"]
        if off >= 4:
            return httpx.Response(422, json={})
        return httpx.Response(200, json={"refineSearch": {"totalHits": 6, "data": {"jobs": many[off:off + 2]}}})

    jobs = await _run(web, lambda c: fetch_phenom(c, SLUG, "X"), handler)
    assert [j.external_id for j in jobs] == [f"{HOST}:R-{i}" for i in range(4)]


async def test_robots_blocking_job_pages_keeps_listing_without_page_fetches(web):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /us/en/job/\n")
        if req.method == "GET":
            raise AssertionError("job page fetched despite robots.txt")
        return _handler()(req)

    jobs = await _run(web, lambda c: fetch_phenom(c, SLUG, "Mastercard"), handler)
    assert len(jobs) == 3 and jobs[0].description_text.startswith("Internal alignment")


@pytest.mark.parametrize("body", [[], {"other": 1}, {"refineSearch": "x"}])
async def test_unexpected_first_page_raises(web, body):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, json=body)

    with pytest.raises(TypeError):
        await _run(web, lambda c: fetch_phenom(c, SLUG, "X"), handler)


def test_detect_prefers_base_url_over_path():
    # a single-segment locale path (/en/job/...) would otherwise look like country "en", lang "job"
    assert detect_phenom("https://careers.mastercard.com/en/job/R-1", JOB_HTML) == ("phenom", SLUG)
    html = JOB_HTML.replace('"https://careers.mastercard.com/us/en/"', '"https://Careers.Mastercard.com/GLOBAL/en/home"')
    assert detect_phenom("https://careers.mastercard.com/", html) == ("phenom", "careers.mastercard.com__global__en")


def test_ld_type_list_and_telecommute():
    page = ('<script type="application/ld+json">{"@type":["JobPosting"],"title":"T","description":"&lt;p&gt;Hi&lt;/p&gt;",'
            '"jobLocationType":["TELECOMMUTE"]}</script>')
    (j,) = parse_phenom([{"jobId": "R-1", "title": "T", "detail": job_posting_ld(page)}], "X", SLUG)
    assert j.description_text == "Hi" and j.workplace_type == "remote"


def test_board_name_comes_from_the_domain():
    from jobfinder.services.resolver import name_from_slug

    assert name_from_slug("phenom", SLUG) == "Mastercard"
    assert name_from_slug("phenom", "jobs.thermofisher.com__global__en") == "Thermofisher"


async def test_invalid_slug_raises_before_network(web):
    with pytest.raises(ValueError):
        await _run(web, lambda c: fetch_phenom(c, "nonsense", "X"), _handler())
