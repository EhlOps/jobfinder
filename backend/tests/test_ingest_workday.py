import asyncio
import json
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import httpx
import pytest

from jobfinder.ingest.discovery import board_from_url
from jobfinder.ingest.sources import ATS_FETCHERS, PROBE_FETCHERS
from jobfinder.ingest.sources.workday import (
    fetch_workday,
    parse_posted_on,
    parse_slug,
    parse_workday,
)

FIX = Path(__file__).parent / "fixtures"
SLUG = "acme.wd1.EXT"
NOW = datetime(2026, 10, 13, 15, 30, tzinfo=UTC)
DATA = json.loads((FIX / "workday_sample.json").read_text())


def test_registered():
    assert ATS_FETCHERS["workday"] is fetch_workday
    assert isinstance(PROBE_FETCHERS["workday"], partial) and PROBE_FETCHERS["workday"].keywords == {"list_only": True}


def test_parse_slug():
    assert parse_slug("boeing.wd1.EXTERNAL_CAREERS") == ("boeing", "wd1", "EXTERNAL_CAREERS")
    assert parse_slug("x.WD12.a.b") == ("x", "wd12", "a.b")
    for bad in ("boeing", "boeing.wd1", "boeing.foo.site", "a/b.wd1.site", "boeing.wd1.", ".wd1.site"):
        with pytest.raises(ValueError):
            parse_slug(bad)


def test_parse_posted_on():
    assert parse_posted_on("Posted Today", NOW) == datetime(2026, 10, 13, tzinfo=UTC)
    assert parse_posted_on("Posted Yesterday", NOW) == datetime(2026, 10, 12, tzinfo=UTC)
    assert parse_posted_on("Posted 5 Days Ago", NOW) == datetime(2026, 10, 8, tzinfo=UTC)
    assert parse_posted_on("Posted 30+ Days Ago", NOW) is None
    assert parse_posted_on("something else", NOW) is None and parse_posted_on(None, NOW) is None


def test_parse_with_details():
    items = [{**p, "detail": DATA["details"].get(p["externalPath"].rsplit("/", 1)[-1], {}).get("jobPostingInfo")}
             for p in DATA["list"]["jobPostings"]]
    jobs = parse_workday(items, "Acme", SLUG, NOW)
    assert [j.external_id for j in jobs] == [
        "acme:Senior-Avionics-Software-Engineer_JR100-1", "acme:Mechanical-Engineer-Intern_JR101",
        "acme:Project-Manager_JR102",
    ]
    a, b, c = jobs
    assert a.source == "workday" and a.seniority == "senior" and a.workplace_type == "hybrid"
    assert a.url == "https://acme.wd1.myworkdayjobs.com/EXT/job/USA---Huntsville-AL/Senior-Avionics-Software-Engineer_JR100-1"
    assert a.employment_type == "Full time" and a.posted_at == datetime(2026, 10, 13, tzinfo=UTC)
    assert "flight software" in a.description_text and "<p>" not in a.description_text
    assert (a.salary_min, a.salary_max) == (120000, 160000)
    # a javascript: externalUrl is rejected in favour of the constructed apply URL
    assert b.url == "https://acme.wd1.myworkdayjobs.com/EXT/job/USA---El-Segundo-CA/Mechanical-Engineer-Intern_JR101"
    assert b.seniority == "intern" and b.workplace_type == "onsite"
    # no detail: title/location only
    assert c.description_text == "" and c.location == "2 Locations" and c.posted_at is None


def test_parse_skips_untitled_and_pathless():
    jobs = parse_workday([{"title": " ", "externalPath": "/job/x"}, {"title": "T"}, {"title": "OK", "externalPath": "/job/a/OK_1"}], "X", SLUG)
    assert [j.title for j in jobs] == ["OK"]


def _handler(fail_details=False, calls=None, offset_cap=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append((req.method, req.url.path))
        assert req.url.host == "acme.wd1.myworkdayjobs.com"
        if req.method == "POST" and req.url.path == "/wday/cxs/acme/EXT/jobs":
            body = json.loads(req.content)
            assert body["limit"] == 20 and body["appliedFacets"] == {}
            off = body["offset"]
            if offset_cap is not None and off >= offset_cap:
                return httpx.Response(400, json={"errorCode": "bad offset"})
            page = DATA["list"]["jobPostings"][off:off + 20]
            # Workday only reports the total on the first page
            return httpx.Response(200, json={"total": 3 if off == 0 else 0, "jobPostings": page})
        if req.method == "GET" and req.url.path.startswith("/wday/cxs/acme/EXT/job/"):
            if fail_details:
                return httpx.Response(500)
            key = req.url.path.rsplit("/", 1)[-1]
            if key in DATA["details"]:
                return httpx.Response(200, json=DATA["details"][key])
            return httpx.Response(404)
        return httpx.Response(404)

    return handler


def _run(coro_fn, handler):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await coro_fn(client)

    return asyncio.run(go())


def test_fetch_with_details_tolerates_a_failed_detail():
    jobs = _run(lambda c: fetch_workday(c, SLUG, "Acme"), _handler())
    assert len(jobs) == 3
    assert "flight software" in jobs[0].description_text and jobs[2].description_text == ""  # 404 detail kept


def test_fetch_list_only_makes_no_detail_calls():
    calls: list = []
    jobs = _run(lambda c: fetch_workday(c, SLUG, "Acme", list_only=True), _handler(calls=calls))
    assert len(jobs) == 3 and all(m == "POST" for m, _ in calls) and len(calls) == 1
    assert all(j.description_text == "" for j in jobs)


def test_fetch_raises_when_every_detail_fails():
    with pytest.raises(RuntimeError, match="detail requests failed"):
        _run(lambda c: fetch_workday(c, SLUG, "Acme"), _handler(fail_details=True))


def test_paging_stops_gracefully_at_offset_cap():
    # total claims more than the cap allows: page 2 is refused, page 1 results are kept
    data = {"total": 40, "jobPostings": DATA["list"]["jobPostings"] * 7}  # 21 items

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            off = json.loads(req.content)["offset"]
            if off == 0:
                return httpx.Response(200, json={**data, "jobPostings": data["jobPostings"][:20]})
            return httpx.Response(400)
        return httpx.Response(200, json={"jobPostingInfo": {}})

    jobs = _run(lambda c: fetch_workday(c, SLUG, "Acme"), handler)
    assert len(jobs) == 20


def test_first_page_error_propagates():
    handler = lambda req: httpx.Response(404)
    with pytest.raises(httpx.HTTPStatusError):
        _run(lambda c: fetch_workday(c, SLUG, "Acme"), handler)


def test_invalid_slug_raises():
    with pytest.raises(ValueError):
        _run(lambda c: fetch_workday(c, "nonsense", "Acme"), _handler())


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://boeing.wd1.myworkdayjobs.com/EXTERNAL_CAREERS/job/USA---Huntsville-AL/X_JR1", ("workday", "boeing.wd1.EXTERNAL_CAREERS")),
        ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite", ("workday", "nvidia.wd5.NVIDIAExternalCareerSite")),
        ("https://x.wd12.myworkdayjobs.com/", None),
        ("https://x.wd1.myworkdayjobs.com/wday/cxs/x/Site/jobs", None),
        ("https://x.myworkdayjobs.com/Site", None),
    ],
)
def test_discovery_maps_workday_urls(url, expected):
    assert board_from_url(url) == expected


def test_unsafe_external_path_is_skipped():
    items = [
        {"title": "A", "externalPath": "/job/x/A_1"},
        {"title": "B", "externalPath": "/../../etc/B_2"},
        {"title": "C", "externalPath": "/job/x/C_3?q=1"},
    ]
    assert [j.title for j in parse_workday(items, "Acme", SLUG, NOW)] == ["A"]
