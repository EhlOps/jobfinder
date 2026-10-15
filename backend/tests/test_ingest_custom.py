"""U7: big-tech custom careers sites are gated on a terms/robots review (sources/custom/TERMS.md)."""
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from factories import make_user
from security_payloads import PRIVATE_URLS, SCRIPT_URLS, SQLI

from jobfinder.ingest import http_safety, resolve
from jobfinder.ingest.discovery import board_from_url, boards_from_text
from jobfinder.ingest.http_safety import FetchError, safe_fetch
from jobfinder.ingest.resolve import resolve_link
from jobfinder.ingest.resolve.ladder import BoardHit, ResolveContext
from jobfinder.ingest.resolve.rungs.url_pattern import UrlPatternRung
from jobfinder.ingest.sources import ATS_FETCHERS, PROBE_FETCHERS
from jobfinder.ingest.sources.custom import (
    CUSTOM_SITES,
    board_for_url,
    refusal_for_url,
    site_for_url,
)
from jobfinder.ingest.validate import SLUG_RE
from jobfinder.models import JobLink, Task

TERMS = Path(__file__).parents[1] / "jobfinder/ingest/sources/custom/TERMS.md"

REFUSED_URLS = {
    "Google": [
        "https://www.google.com/about/careers/applications/jobs/results/1234-software-engineer",
        "https://google.com/about/careers/applications/jobs/results?q=sre",
        "https://careers.google.com/jobs/results/1234/",
        "https://www.google.com/intl/en/about/careers/applications/",
        "https://www.google.co.uk/about/careers/applications/jobs/results/1",
        "https://www.google.com/url?q=https://jobs.apple.com/x",
    ],
    "Apple": ["https://jobs.apple.com/en-us/details/200512345/software-engineer",
              "https://JOBS.APPLE.COM/en-gb/search", "https://www.apple.com/careers/us/"],
    "Amazon": ["https://www.amazon.jobs/en/jobs/2712345/sde-ii", "https://amazon.jobs/en/search?q=x",
               "https://hiring.amazon.com/job-opportunities", "https://www.amazon.co.uk/jobs"],
    "Meta": ["https://www.metacareers.com/jobs/123456789/", "https://metacareers.com/jobs?q=ml",
             "https://www.facebook.com/careers/jobs/123"],
    "Tesla": ["https://www.tesla.com/careers/search/job/software-engineer-123456",
              "https://www.tesla.com/x/../careers/search", "https://www.tesla.com/c%61reers/search"],
    "IBM": ["https://www.ibm.com/careers/search?q=x", "https://careers.ibm.com/job/123/"],
    "Uber": ["https://www.uber.com/global/en/careers/list/123456/", "https://www.uber.com/us/en/careers/"],
    "Goldman Sachs": ["https://higher.gs.com/roles/123456"],
    "Citadel": ["https://www.citadel.com/careers/details/software-engineer/"],
}


def test_every_site_has_a_recorded_decision():
    names = [s.name for s in CUSTOM_SITES]
    assert len(names) == len(set(names))
    text = TERMS.read_text()
    assert "2026-10" in text
    for s in CUSTOM_SITES:
        assert s.reason and s.hosts and s.reviewed.startswith("2026-10")
        assert s.name in text, f"{s.name} missing from TERMS.md"
        if not s.terms_ok:
            assert s.ats is None and s.board is None, f"refused {s.name} must not have an adapter or board"
    for big in ("Google", "Apple", "Amazon", "Meta"):
        assert big in names


def test_refused_sites_are_never_registered():
    refused = {s.name.lower().replace(" ", "_") for s in CUSTOM_SITES if not s.terms_ok}
    keys = set(ATS_FETCHERS) | set(PROBE_FETCHERS)
    for key in keys:
        assert not any(key.startswith(r) for r in refused), key
    for k in ("google_careers", "apple_jobs", "amazon_jobs", "meta_careers", "tesla", "ibm"):
        assert k not in keys
    for s in CUSTOM_SITES:  # an allowed custom adapter must actually be registered
        if s.ats:
            assert s.terms_ok and s.ats in ATS_FETCHERS and s.board and s.board[0] == s.ats


@pytest.mark.parametrize("name,url", [(n, u) for n, us in REFUSED_URLS.items() for u in us])
def test_refused_sites_are_not_recognised(name, url):
    site = refusal_for_url(url)
    assert site is not None and site.name == name
    assert board_from_url(url) is None and board_for_url(url) is None
    assert boards_from_text(f"apply here: {url} thanks") == []


async def test_url_pattern_names_the_company():
    ctx = ResolveContext("https://www.janestreet.com/join-jane-street/position/1/", httpx.AsyncClient())
    assert await UrlPatternRung().run(ctx) == BoardHit("greenhouse", "janestreet", name="Jane Street")
    ctx = ResolveContext("https://boards.greenhouse.io/acme/jobs/1", httpx.AsyncClient())
    assert await UrlPatternRung().run(ctx) == BoardHit("greenhouse", "acme")


def test_allowed_sites_map_to_existing_boards():
    js = "https://www.janestreet.com/join-jane-street/position/8631912002/"
    assert board_from_url(js) == ("greenhouse", "janestreet")
    assert board_from_url("https://www.janestreet.com/join-jane-street/open-roles/") == ("greenhouse", "janestreet")
    assert board_from_url("https://jobs.intel.com/en/job/santa-clara/engineer/599/123") == (
        "workday", "intel.wd1.External")
    for s in CUSTOM_SITES:
        if s.board:
            ats, slug = s.board
            assert ats in ATS_FETCHERS and SLUG_RE.match(slug)
    assert refusal_for_url(js) is None


def test_matching_is_by_host_and_path_not_substring():
    for url in [
        "https://google.example.io/careers",  # not a Google country domain
        "https://www.googleapis.example/about/careers",
        "https://www.tesla.com/careersx",
        "https://notapple.com/jobs.apple.com",
        "https://jobs.apple.com.evil.example/en-us/details/1",
        "https://evil-amazon.jobs.example.com/x",
        "https://www.tesla.com/model3",
        "https://www.janestreet.com/what-we-do/",  # not the careers path
        "https://www.uber.com/us/en/ride/",
        "https://www.uber.com/a/b/c/careers/",  # too deep to be a locale prefix
    ]:
        assert site_for_url(url) is None, url
    assert site_for_url("https://amazon.jobs.") is not None  # trailing-dot host is still Amazon


@pytest.mark.parametrize("url", [*PRIVATE_URLS, *SCRIPT_URLS, "", "not a url", "http://[::1", "ftp://jobs.apple.com/x"])
def test_hostile_urls_are_ignored(url):
    assert site_for_url(url) is None
    assert board_for_url(url) is None


@pytest.mark.parametrize("payload", SQLI)
def test_sqli_in_path_does_not_change_the_fixed_board(payload):
    url = f"https://www.janestreet.com/join-jane-street/position/{payload}"
    assert board_from_url(url) in (("greenhouse", "janestreet"), None)
    assert refusal_for_url(f"https://www.amazon.jobs/en/jobs/{payload}") is not None


async def test_pasted_refused_link_never_reaches_a_rung_or_the_network(maker, monkeypatch):
    calls = []

    class Spy:
        name, order = "spy", 1.0

        async def run(self, ctx):
            calls.append(ctx.url)

    monkeypatch.setattr(resolve, "RUNGS", [Spy()])

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text="<html></html>")

    async with maker() as db, httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        u = await make_user(db)
        link = JobLink(user_id=u.id, url="https://www.amazon.jobs/en/jobs/2712345/sde-ii", status="resolving")
        db.add(link)
        await db.commit()
        out = await resolve_link(db, link.id, client=c)
        await db.refresh(link)
    assert calls == []
    assert out == {"status": "unresolved", "reason": "terms refused", "site": "Amazon"}
    assert link.status == "unresolved" and link.rung == "terms_refused"
    assert "Amazon" in link.error and "Conditions of Use" in link.error and link.company_id is None


@pytest.fixture
def public_dns(monkeypatch):
    async def fake(url):
        return httpx.URL(url).host, "93.184.216.34"

    monkeypatch.setattr(http_safety, "resolve_public", fake)


async def test_safe_fetch_refuses_a_redirect_onto_a_refused_site(public_dns):
    seen = []

    def handler(request):
        host = request.headers["host"]
        seen.append(host)
        if host == "short.example":
            return httpx.Response(301, headers={"location": "https://jobs.apple.com/en-us/details/1"})
        return httpx.Response(200, text="apple page")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(FetchError, match="Apple"):
            await safe_fetch(c, "https://short.example/abc")
        with pytest.raises(FetchError, match="Amazon"):
            await safe_fetch(c, "https://www.amazon.jobs/en/search.json")
    assert seen == ["short.example"]


async def test_failed_custom_board_stops_the_ladder(maker, monkeypatch):
    later = []

    class Later:
        name, order = "page_rung", 2.0

        async def run(self, ctx):
            later.append(ctx.url)

    monkeypatch.setattr(resolve, "RUNGS", [UrlPatternRung(), Later()])
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404, json={})))
    async with maker() as db:
        u = await make_user(db)
        link = JobLink(user_id=u.id, url="https://jobs.intel.com/en/job/x/y/599/1", status="resolving")
        db.add(link)
        await db.commit()
        out = await resolve_link(db, link.id, client=client)
        await db.refresh(link)
    assert later == [] and out["status"] == "unresolved"
    assert link.ats == "workday" and link.slug == "intel.wd1.External" and "couldn't be read" in link.error


async def test_api_answers_refused_links_without_queueing(authed, maker):
    r = await authed.post("/api/job-links", json={"url": "https://jobs.apple.com/en-us/details/200512345/x"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "unresolved" and body["rung"] == "terms_refused"
    assert "Apple" in body["error"] and body["company"] is None
    async with maker() as db:
        assert (await db.scalars(sa.select(Task).where(Task.kind == "resolve_job_link"))).all() == []
