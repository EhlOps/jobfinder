import json
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import httpx
import pytest
from security_payloads import SCRIPT_URLS, SQLI

from jobfinder.integrations.ats import SLUG_RE, oracle_hcm
from jobfinder.integrations.ats.detect import detect_board
from jobfinder.integrations.ats.oracle_hcm import (
    detect_ce_page,
    fetch_oracle_hcm,
    parse_oracle_hcm,
    parse_slug,
)
from jobfinder.integrations.ats.registry import ATS_FETCHERS, PROBE_FETCHERS
from jobfinder.integrations.directories import board_from_url
from tests.fakes import handle

FIX = Path(__file__).parent / "fixtures"
SLUG = "eeho.fa.us2__CX_45001"
HOST = "eeho.fa.us2.oraclecloud.com"
DATA = json.loads((FIX / "oracle_hcm_sample.json").read_text())
LISTED = DATA["list"]["items"][0]["requisitionList"]
LIST_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
DETAIL_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"


def test_registered():
    assert ATS_FETCHERS["oracle_hcm"] is fetch_oracle_hcm
    probe = PROBE_FETCHERS["oracle_hcm"]
    assert isinstance(probe, partial) and probe.keywords == {"list_only": True}


def test_parse_slug():
    assert parse_slug(SLUG) == (HOST, "CX_45001")
    assert parse_slug("hdpc-test.fa.em2__jobsearch") == ("hdpc-test.fa.em2.oraclecloud.com", "jobsearch")
    assert SLUG_RE.match(SLUG)


@pytest.mark.parametrize("bad", [
    "", "eeho.fa.us2", "eeho.fa.us2__", "__CX_1", "eeho.us2__CX_1", "a.b.fa.us2__CX_1",
    "EEHO.fa.us2__CX_1", "eeho.fa.us2__CX-1", "eeho.fa.us2__CX_1/../x", "eeho.fa.us2__" + "x" * 41,
    "evil.com#.fa.us2__CX_1", "eeho.fa.us2__CX_1?x=1", *SQLI,
])
def test_malformed_slugs_refused(bad):
    with pytest.raises(ValueError):
        parse_slug(bad)


@pytest.mark.parametrize("url,expected", [
    (f"https://{HOST}/hcmUI/CandidateExperience/en/sites/CX_45001/job/345147", ("oracle_hcm", SLUG)),
    (f"https://{HOST}/hcmUI/CandidateExperience/en/sites/CX_45001/requisitions", ("oracle_hcm", SLUG)),
    (f"https://{HOST}/hcmUI/CandidateExperience/sites/CX_45001/job/345147?utm=x", ("oracle_hcm", SLUG)),
    (f"https://{HOST}/hcmUI/CandidateExperience/en-GB/sites/CX_45001/requisitions/preview/1",
     ("oracle_hcm", SLUG)),
    ("https://EEHO.FA.US2.oraclecloud.com/hcmUI/CandidateExperience/fr/sites/jobsearch/job/1",
     ("oracle_hcm", "eeho.fa.us2__jobsearch")),
    (f"https://{HOST}/hcmUI/CandidateExperience/en/sites/", None),
    (f"https://{HOST}/hcmUI/CandidateExperience/en/job/1", None),
    (f"https://{HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitions", None),
    (f"https://{HOST}/hcmUI/CandidateExperience/en/sites/CX-1;drop/job/1", None),
    ("https://a.b.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1", None),
    ("https://cloud.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1", None),
    ("https://eeho.fa.us2.oraclecloud.com.evil.example/hcmUI/CandidateExperience/en/sites/CX_1", None),
    (f"https://{HOST}/hcmUI/candidateexperience/en/Sites/CX_45001/job/1", ("oracle_hcm", SLUG)),
    ("https://careers.oracle.com/jobs/#en/sites/jobsearch/job/345147", None),  # custom domain: HTML detector
])
def test_board_from_url(url, expected):
    assert board_from_url(url) == expected


@pytest.mark.parametrize("payload", SQLI + SCRIPT_URLS)
def test_hostile_urls_are_not_boards(payload):
    assert board_from_url(f"https://{HOST}/hcmUI/CandidateExperience/en/sites/{payload}/job/1") is None
    assert board_from_url(payload) is None


def test_detects_custom_domain_page():
    html = (FIX / "oracle_hcm_custom_domain.html").read_text()
    assert detect_ce_page("https://careers.oracle.com/en/sites/jobsearch", html) == ("oracle_hcm", SLUG)
    assert detect_board("https://careers.oracle.com/en/sites/jobsearch", html) == ("oracle_hcm", SLUG)


@pytest.mark.parametrize("html", [
    "<html><body>no base tag here, CX_45001</body></html>",
    '<base href="/" data-sitenumber="CX_1">',  # no api host
    '<base data-apibaseurl="https://evil.example" data-sitenumber="CX_1">',  # not oraclecloud
    '<base data-apibaseurl="http://eeho.fa.us2.oraclecloud.com" data-sitenumber="CX_1">',  # not https
    '<base data-apibaseurl="https://eeho.fa.us2.oraclecloud.com.evil.example" data-sitenumber="CX_1">',
    '<base data-apibaseurl="https://eeho.fa.us2.oraclecloud.com" data-sitenumber="CX_1\';DROP">',
    '<base data-apibaseurl="javascript:alert(1)" data-sitenumber="CX_1">',
])
def test_detector_ignores_other_pages(html):
    assert detect_ce_page("https://careers.acme.com/", html) is None


def _with_details(items):
    return [{**it, "detail": (DATA["details"].get(it.get("Id")) or {}).get("items", [{}])[0]} for it in items]


def test_parse_with_details():
    jobs = parse_oracle_hcm(_with_details(LISTED), "Oracle", SLUG)
    assert [j.external_id for j in jobs] == ["eeho.fa.us2:347559", "eeho.fa.us2:347562", "eeho.fa.us2:345147"]
    a, b, c = jobs
    assert all(j.source == "oracle_hcm" for j in jobs)
    assert a.url == f"https://{HOST}/hcmUI/CandidateExperience/en/sites/CX_45001/job/347559"
    assert a.title.startswith("Senior Support Engineer") and a.seniority == "senior"
    assert a.location == "Houston, TX, United States; United States; Austin, TX, United States"
    assert a.posted_at == datetime(2026, 10, 14, tzinfo=UTC)
    assert "Role Purpose" in a.description_text and "Triage product data defects" in a.description_text
    assert "<p>" not in a.description_text and "alert(1)" not in a.description_text
    assert (a.salary_min, a.salary_max, a.salary_currency) == (96800, 151100, "USD")
    assert a.employment_type == "Full time"
    # identical description/responsibilities sections appear once; declared workplace type wins
    assert b.description_text == "Build mission-critical Java backend software for OCI."
    assert b.workplace_type == "remote" and b.seniority == "staff"
    # no detail: listing data and the short description
    assert c.workplace_type == "hybrid" and c.location.endswith("KS, United States")
    assert c.description_text.startswith("The Oracle Health Configuration Center")
    assert c.posted_at == datetime(2026, 10, 13, tzinfo=UTC)


def test_parse_skips_untitled_and_bad_ids():
    jobs = parse_oracle_hcm(
        [{"Id": "1", "Title": " "}, {"Title": "No id"}, {"Id": "a/b", "Title": "Slash"},
         {"Id": "x\"onmouseover", "Title": "Quote"}, {"Id": 42, "Title": "OK"}], "X", SLUG)
    assert [(j.external_id, j.title) for j in jobs] == [("eeho.fa.us2:42", "OK")]
    assert jobs[0].posted_at is None and jobs[0].description_text == ""


def _handler(calls=None, fail_details=False, total=None, pages=None):
    """Serves the fixture listing; `pages` (a list of requisition lists) overrides it for paging tests."""
    def handler(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(req.url))
        assert req.headers["host"] == HOST
        q = req.url.query.decode()
        if req.url.path == LIST_PATH:
            finder = dict(kv.split("=", 1) for kv in q.split("finder=findReqs;", 1)[1].split(","))
            assert finder["siteNumber"] == "CX_45001" and finder["sortBy"] == "POSTING_DATES_DESC"
            limit, offset = int(finder["limit"]), int(finder["offset"])
            if pages is None:
                body = DATA["list"]
            else:
                starts = [sum(len(pg) for pg in pages[:i]) for i in range(len(pages))]
                reqs = pages[starts.index(offset)] if offset in starts else []
                assert len(reqs) <= limit
                body = {"items": [{"TotalJobsCount": total or 0, "requisitionList": reqs}]}
            return httpx.Response(200, json=body)
        if req.url.path == DETAIL_PATH:
            if fail_details:
                return httpx.Response(500)
            rid = q.split('Id=%22', 1)[1].split("%22", 1)[0]
            if rid in DATA["details"]:
                return httpx.Response(200, json=DATA["details"][rid])
            return httpx.Response(404)
        return httpx.Response(404)

    return handler


async def _run(web, handler, **kw):
    handle(web, handler)
    async with httpx.AsyncClient() as client:
        return await fetch_oracle_hcm(client, SLUG, "Oracle", **kw)


async def test_request_urls_are_exact(web):
    calls: list[str] = []
    await _run(web, _handler(calls=calls))
    assert calls[0] == (
        f"https://{HOST}{LIST_PATH}?onlyData=true&expand=requisitionList.secondaryLocations"
        "&finder=findReqs;siteNumber=CX_45001,limit=50,offset=0,sortBy=POSTING_DATES_DESC"
    )
    assert f"https://{HOST}{DETAIL_PATH}?expand=all&onlyData=true&finder=ById;Id=%22347559%22,siteNumber=CX_45001" \
        in calls
    # the hostile id from the listing never reaches a request
    assert len(calls) == 1 + 3 and not any("admin" in c for c in calls)


async def test_fetch_with_details_tolerates_a_failed_detail(web):
    jobs = await _run(web, _handler())
    assert len(jobs) == 3
    assert "Role Purpose" in jobs[0].description_text
    assert jobs[2].description_text.startswith("The Oracle Health")  # 404 detail: short description kept


async def test_list_only_makes_one_request(web):
    calls: list[str] = []
    jobs = await _run(web, _handler(calls=calls), list_only=True)
    assert len(jobs) == 3 and len(calls) == 1 and LIST_PATH in calls[0]
    assert jobs[0].description_text.startswith("Resolve critical product data issues")


async def test_raises_when_every_detail_fails(web):
    with pytest.raises(RuntimeError, match="detail requests failed"):
        await _run(web, _handler(fail_details=True))


def _page(start, n):
    return [{"Id": str(i), "Title": f"Engineer {i}"} for i in range(start, start + n)]


async def test_paging_stops_at_total(web, monkeypatch):
    monkeypatch.setattr(oracle_hcm, "MAX_DETAILS", 0)
    calls: list[str] = []
    pages = [_page(0, 50), _page(50, 50), _page(100, 50)]
    jobs = await _run(web, _handler(calls=calls, total=100, pages=pages))
    assert len(jobs) == 100 and len(calls) == 2
    assert "offset=50," in calls[1]


async def test_paging_stops_on_empty_page_or_short_page_without_total(web, monkeypatch):
    monkeypatch.setattr(oracle_hcm, "MAX_DETAILS", 0)
    calls: list[str] = []
    # TotalJobsCount over-reports: an empty page ends paging
    jobs = await _run(web, _handler(calls=calls, total=1000, pages=[_page(0, 50), _page(50, 7)]))
    assert len(jobs) == 57 and len(calls) == 3
    calls.clear()
    # no total: a short page is the last one
    jobs = await _run(web, _handler(calls=calls, total=None, pages=[_page(0, 50), _page(50, 7)]))
    assert len(jobs) == 57 and len(calls) == 2


async def test_paging_steps_by_rows_returned(web, monkeypatch):
    # a pod that caps `limit` at 25 still gets paged completely
    monkeypatch.setattr(oracle_hcm, "MAX_DETAILS", 0)
    calls: list[str] = []
    jobs = await _run(web, _handler(calls=calls, total=60, pages=[_page(0, 25), _page(25, 25), _page(50, 10)]))
    assert len(jobs) == 60 and [c.split("offset=")[1].split(",")[0] for c in calls] == ["0", "25", "50"]


async def test_paging_is_capped(web, monkeypatch):
    monkeypatch.setattr(oracle_hcm, "MAX_DETAILS", 0)
    monkeypatch.setattr(oracle_hcm, "MAX_PAGES", 3)
    calls: list[str] = []
    jobs = await _run(web, _handler(calls=calls, total=10_000, pages=[_page(i * 50, 50) for i in range(10)]))
    assert len(calls) == 3 and len(jobs) == 150


async def test_paging_stops_when_offset_is_ignored(web, monkeypatch):
    monkeypatch.setattr(oracle_hcm, "MAX_DETAILS", 0)
    calls: list[str] = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, json={"items": [{"TotalJobsCount": 10_000, "requisitionList": _page(0, 50)}]})

    jobs = await _run(web, handler)
    assert len(calls) == 2 and len(jobs) == 50  # no duplicates, no loop to MAX_PAGES


async def test_string_total_is_tolerated(web, monkeypatch):
    monkeypatch.setattr(oracle_hcm, "MAX_DETAILS", 0)
    jobs = await _run(web, lambda r: httpx.Response(200, json={"items": [{"TotalJobsCount": "3", "requisitionList": _page(0, 3)}]}))
    assert len(jobs) == 3


async def test_one_failed_detail_on_a_tiny_board_keeps_the_posting(web):
    def handler(req):
        if req.url.path == LIST_PATH:
            return httpx.Response(200, json={"items": [{"TotalJobsCount": 1, "requisitionList": _page(0, 1)}]})
        return httpx.Response(404)

    jobs = await _run(web, handler)
    assert [j.title for j in jobs] == ["Engineer 0"]


async def test_detail_cap(web, monkeypatch):
    monkeypatch.setattr(oracle_hcm, "MAX_DETAILS", 2)
    calls: list[str] = []
    jobs = await _run(web, _handler(calls=calls))
    assert len(jobs) == 3 and sum(DETAIL_PATH in c for c in calls) == 2


async def test_backs_off_on_429(web, monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(oracle_hcm.asyncio, "sleep", fake_sleep)
    hits = {"n": 0}
    inner = _handler()

    def handler(req):
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return inner(req)

    jobs = await _run(web, handler, list_only=True)
    assert len(jobs) == 3 and sleeps == [2.0]


async def test_first_page_error_propagates(web):
    with pytest.raises(httpx.HTTPStatusError):
        await _run(web, lambda req: httpx.Response(404))


async def test_unexpected_shapes_yield_nothing(web):
    for body in ({}, {"items": []}, {"items": [None]}, {"items": [{"requisitionList": "nope"}]}, []):
        assert await _run(web, lambda req, b=body: httpx.Response(200, json=b), list_only=True) == []


async def test_invalid_slug_makes_no_request(web):
    calls: list[str] = []
    handle(web, _handler(calls=calls))
    for bad in ["nonsense", *SQLI]:
        with pytest.raises(ValueError):
            async with httpx.AsyncClient() as c:
                await fetch_oracle_hcm(c, bad, "X")
    assert calls == []
