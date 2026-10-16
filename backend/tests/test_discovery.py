import asyncio

import httpx
import pytest
import sqlalchemy as sa

from jobfinder import worker
from jobfinder.config import get_settings
from jobfinder.ingest import discovery, http_safety
from jobfinder.ingest.discovery import board_from_url, boards_from_text, github_lists, hn_hiring, yc
from jobfinder.ingest.discovery.robots import USER_AGENT, PoliteFetcher, RobotsDisallowed
from jobfinder.models import Company
from jobfinder.scheduling.handlers import HANDLERS
from tests.fakes import fake_resolve_public, handle, serve


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """robots.txt is fetched through safe_fetch (IP-pinned): no real DNS in unit tests."""
    monkeypatch.setattr(http_safety, "resolve_public", fake_resolve_public())


GH_GOOD = {"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://x/1", "location": {"name": "Remote"}}]}


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://boards.greenhouse.io/acme/jobs/123?gh_src=x", ("greenhouse", "acme")),
        ("https://job-boards.greenhouse.io/Acme", ("greenhouse", "Acme")),
        ("https://boards.greenhouse.io/embed/job_app?for=acme", ("greenhouse", "acme")),
        ("https://apply.workable.com/api/v1/x", None),
        ("https://jobs.lever.co/acme/abc-def/apply", ("lever", "acme")),
        ("https://jobs.ashbyhq.com/acme?utm_source=x", ("ashby", "acme")),
        ("https://apply.workable.com/acme/j/ABC/", ("workable", "acme")),
        ("https://acme.recruitee.com/o/engineer", ("recruitee", "acme")),
        ("https://www.recruitee.com/", None),
        ("https://acme.bamboohr.com/careers/12", ("bamboohr", "acme")),
        ("https://jobs.smartrecruiters.com/AcmeCorp/123-eng", ("smartrecruiters", "AcmeCorp")),
        ("https://boards.greenhouse.io/", None),
        ("https://example.com/acme", None),
        ("ftp://jobs.lever.co/acme", None),
    ],
)
def test_board_from_url(url, expected):
    assert board_from_url(url) == expected


def test_boards_from_text_dedupes_and_ignores_noise():
    text = (
        "[Apply](https://jobs.lever.co/acme/1) <a href=\"https://jobs.lever.co/ACME/2\">x</a> "
        "see https://boards.greenhouse.io/beta/jobs/9, and https://example.com/z"
    )
    assert boards_from_text(text) == [("lever", "acme"), ("greenhouse", "beta")]


def robots_site(robots: str | None, routes: dict[str, httpx.Response] | None = None, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        assert request.headers["user-agent"] == USER_AGENT
        if request.url.path == "/robots.txt":
            return httpx.Response(404) if robots is None else httpx.Response(200, text=robots)
        for key, resp in (routes or {}).items():
            if request.url.path.startswith(key) or str(request.url).startswith(key):
                return resp
        return httpx.Response(404)

    return handler


async def test_robots_disallow_blocks_fetch(web):
    seen: list = []
    handler = robots_site("User-agent: *\nDisallow: /\n", {"/": httpx.Response(200, text="x")}, seen)
    handle(web, handler)
    async with httpx.AsyncClient() as c:
        f = PoliteFetcher(c)
        with pytest.raises(RobotsDisallowed):
            await f.get("https://raw.githubusercontent.com/a/b/dev/README.md")
    assert [r.url.path for r in seen] == ["/robots.txt"]  # the page itself was never requested


async def test_robots_missing_allows_and_unreachable_blocks(web):
    handler = robots_site(None, {"/ok": httpx.Response(200, text="hi")})
    handle(web, handler)
    async with httpx.AsyncClient() as c:
        assert (await PoliteFetcher(c).get("https://example.com/ok")).text == "hi"

    serve(web, status=503)
    async with httpx.AsyncClient() as c:
        assert not await PoliteFetcher(c).allowed("https://example.com/ok")


async def test_robots_partial_disallow(web):
    handler = robots_site("User-agent: *\nDisallow: /api/\n")
    handle(web, handler)
    async with httpx.AsyncClient() as c:
        f = PoliteFetcher(c)
        assert not await f.allowed("https://example.com/api/v1/x")
        assert await f.allowed("https://example.com/readme")


async def test_github_list_source(web):
    md = "| Acme | [Apply](https://jobs.lever.co/acme/1?utm_source=Simplify&ref=Simplify) |\n"
    handler = robots_site(None, {"/SimplifyJobs": httpx.Response(200, text=md)})
    handle(web, handler)
    async with httpx.AsyncClient() as c:
        assert await github_lists.fetch(PoliteFetcher(c)) == [("lever", "acme")]


async def test_github_lists_cover_many_lists_and_survive_a_dead_one(web):
    listing = '[{"category": "Hardware", "url": "https://job-boards.greenhouse.io/spacey/jobs/9"}]'
    routes = {
        "/SimplifyJobs/New-Grad-Positions/dev/.github": httpx.Response(200, text=listing),
        "/SimplifyJobs/Summer2026-Internships": httpx.Response(500),
        "/speedyapply": httpx.Response(200, text="[Apply](https://jobs.ashbyhq.com/Quant-Co?x=1)"),
    }
    handle(web, robots_site(None, routes))
    async with httpx.AsyncClient() as c:
        boards = await github_lists.fetch(PoliteFetcher(c))
    assert boards == [("greenhouse", "spacey"), ("ashby", "Quant-Co")]
    assert len(github_lists.URLS) == len(set(github_lists.URLS)) > 10


async def test_github_lists_all_failing_raises(web):
    handle(web, robots_site(None))
    async with httpx.AsyncClient() as c:
        with pytest.raises(httpx.HTTPStatusError):
            await github_lists.fetch(PoliteFetcher(c))


async def test_github_lists_partial_failure_is_reported_not_just_logged(maker, web):
    class Boom(Exception):
        pass

    calls = {"n": 0}

    async def flaky(url, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise Boom("weird")
        return httpx.Response(200, text="https://jobs.lever.co/acme/1", request=httpx.Request("GET", url))

    class F:
        get = staticmethod(flaky)

    boards = await github_lists.fetch(F())
    assert list(boards) == [("lever", "acme")] and len(boards.warnings) == 1 and "Boom" in boards.warnings[0]

    async def fetch_partial(_f):
        return boards

    src = discovery.Source("github_lists", "n/a", True, fetch_partial)
    async with maker() as db, router(web, {"acme": httpx.Response(404)}, "") as c:
        res = await discovery.discover_boards(db, c, sources=[src])
    assert res.sources[0].status == "partial" and res.sources[0].warnings == boards.warnings
    assert res.as_dict()["sources"][0]["warnings"] == boards.warnings


async def test_over_cap_candidates_are_shuffled(maker, monkeypatch, web):
    md = " ".join(f"https://boards.greenhouse.io/b{i}" for i in range(5))
    boards = {f"b{i}": httpx.Response(200, json=GH_GOOD) for i in range(5)}
    monkeypatch.setattr(discovery.random, "shuffle", lambda xs: xs.reverse())
    async with maker() as db, router(web, boards, md) as c:
        res = await discovery.discover_boards(db, c, max_candidates=2, sources=[github_lists.SOURCE])
    assert sorted(res.added) == ["greenhouse/b3", "greenhouse/b4"]


async def test_hn_source_picks_latest_thread_and_unescapes(web):
    search = {"hits": [
        {"objectID": "2", "title": "Ask HN: Who wants to be hired? (May 2026)"},
        {"objectID": "3", "title": "Ask HN: Who is hiring? (May 2026)"},
        {"objectID": "1", "title": "Ask HN: Who is hiring? (April 2026)"},
    ]}
    item = {"children": [
        {"text": 'Acme | <a href="https:&#x2F;&#x2F;boards.greenhouse.io&#x2F;acme&#x2F;jobs&#x2F;1" rel="nofollow">apply</a>'},
        {"text": "Beta https://jobs.ashbyhq.com/beta"},
        {"text": None},
    ]}
    asked: list[str] = []

    def handler(request):
        asked.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path.endswith("/search_by_date"):
            return httpx.Response(200, json=search)
        return httpx.Response(200, json=item)

    handle(web, handler)
    async with httpx.AsyncClient() as c:
        boards = await hn_hiring.fetch(PoliteFetcher(c))
    assert boards == [("greenhouse", "acme"), ("ashby", "beta")]
    assert "/api/v1/items/3" in asked


def router(web, boards: dict[str, httpx.Response], md: str):
    """One transport for the whole run: robots, the list source and the ATS board APIs."""

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        if host == "raw.githubusercontent.com":
            return httpx.Response(200, text=md)
        if host == "boards-api.greenhouse.io":
            slug = path.split("/")[3]
            return boards.get(slug, httpx.Response(404, json={}))
        return httpx.Response(404, json={})

    handle(web, handler)
    return httpx.AsyncClient()


async def test_discovery_validates_dedupes_and_adds(maker, web):
    md = (
        "https://boards.greenhouse.io/acme/jobs/1 https://boards.greenhouse.io/ACME/jobs/2 "
        "https://boards.greenhouse.io/empty https://boards.greenhouse.io/gone https://boards.greenhouse.io/known"
    )
    boards = {
        "acme": httpx.Response(200, json=GH_GOOD),
        "empty": httpx.Response(200, json={"jobs": []}),
        "known": httpx.Response(200, json=GH_GOOD),
    }
    async with maker() as db:
        db.add(Company(name="Known", ats="greenhouse", slug="known"))
        await db.commit()
        async with router(web, boards, md) as c:
            res = await discovery.discover_boards(db, c, max_candidates=10, sources=[github_lists.SOURCE, yc.SOURCE, yc.WELLFOUND])
        rows = (await db.execute(sa.select(Company).order_by(Company.slug))).scalars().all()
    assert res.added == ["greenhouse/acme"]
    assert set(res.rejected) == {"greenhouse/empty", "greenhouse/gone"}
    assert res.candidates == 4 and res.already_known == 1 and res.checked == 3
    assert [(r.slug, r.origin) for r in rows] == [("acme", "discovered"), ("known", "seed")]
    statuses = {s.name: s.status for s in res.sources}
    assert statuses == {"github_lists": "ok", "yc_work_at_a_startup": "skipped", "wellfound": "skipped"}
    assert all(s.reason for s in res.sources if s.status == "skipped")


async def test_discovery_caps_candidates_and_survives_source_errors(maker, web):
    md = " ".join(f"https://boards.greenhouse.io/b{i}" for i in range(5))
    boards = {f"b{i}": httpx.Response(200, json=GH_GOOD) for i in range(5)}
    broken = discovery.Source("broken", "n/a", True, hn_hiring.fetch)

    async with maker() as db, router(web, boards, md) as c:
        # the hn fetch gets a 404 for its API call and must be reported as an error, not abort the run
        res = await discovery.discover_boards(db, c, max_candidates=2, sources=[broken, github_lists.SOURCE])
    assert len(res.added) == 2 and res.checked == 2 and res.candidates == 5
    assert res.sources[0].status == "error" and res.sources[1].status == "ok"


async def test_robots_blocked_source_is_skipped_and_nothing_added(maker, web):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text="User-agent: *\nDisallow: /\n")

    handle(web, handler)
    async with maker() as db, httpx.AsyncClient() as c:
        res = await discovery.discover_boards(db, c, sources=[github_lists.SOURCE])
        assert await db.scalar(sa.select(sa.func.count()).select_from(Company)) == 0
    assert res.sources[0].status == "skipped" and "robots.txt" in res.sources[0].reason
    assert [r.url.path for r in seen] == ["/robots.txt"]


def test_terms_gate_and_scheduling(monkeypatch):
    names = {s.name: s for s in discovery.all_sources()}
    assert not names["wellfound"].terms_ok and not names["yc_work_at_a_startup"].terms_ok
    assert all(s.terms_note for s in names.values())
    assert "discover_boards" in HANDLERS
    settings = get_settings()
    monkeypatch.setattr(settings, "discovery_enabled", False)
    assert "discover_boards" not in {j.id for j in worker.make_scheduler().get_jobs()}
    monkeypatch.setattr(settings, "discovery_enabled", True)
    assert "discover_boards" in {j.id for j in worker.make_scheduler().get_jobs()}


def probe_handler(robots: dict[str, str], stats: dict, delay: float = 0.0, md: str = ""):
    """Handler for bamboohr/smartrecruiters boards that records every URL and the max in-flight board probes."""

    async def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        stats["urls"].append(str(request.url))
        if path == "/robots.txt":  # fetched by validated IP; the site is in the Host header
            return httpx.Response(200, text=robots.get(request.headers["host"], ""))
        if host == "raw.githubusercontent.com":
            return httpx.Response(200, text=md)
        stats["inflight"] += 1
        stats["max"] = max(stats["max"], stats["inflight"])
        await asyncio.sleep(delay)
        stats["inflight"] -= 1
        if host == "api.smartrecruiters.com":
            return httpx.Response(200, json={"totalFound": 1, "content": [{"id": "9", "name": "Eng"}]})
        if host.endswith(".bamboohr.com") and path == "/careers/list":
            return httpx.Response(200, json={"result": [{"id": 1, "jobOpeningName": "Eng"}]})
        return httpx.Response(404, json={})

    return handler


async def test_validate_candidates_list_only_robots_and_bounded_concurrency(web):
    stats = {"urls": [], "inflight": 0, "max": 0}
    boards = [("bamboohr", f"b{i}") for i in range(6)] + [("smartrecruiters", "sr1"), ("bamboohr", "blocked")]
    handler = probe_handler({"blocked.bamboohr.com": "User-agent: *\nDisallow: /\n"}, stats, delay=0.02)
    handle(web, handler)
    async with httpx.AsyncClient() as c:
        results = await discovery.validate_candidates(PoliteFetcher(c), boards, concurrency=3)
    assert [r.ok for r in results] == [True] * 7 + [False]
    assert "robots.txt" in results[-1].reason
    assert not any("/detail" in u or u.endswith("/postings/9") for u in stats["urls"])
    assert not any(u.startswith("https://blocked.bamboohr.com/careers") for u in stats["urls"])
    assert 1 < stats["max"] <= 3


async def test_discover_boards_uses_probe_for_detail_heavy_ats(maker, web):
    stats = {"urls": [], "inflight": 0, "max": 0}
    md = "https://acme.bamboohr.com/careers/1 https://jobs.smartrecruiters.com/Sr1/5"
    handler = probe_handler({}, stats, md=md)
    handle(web, handler)
    async with maker() as db, httpx.AsyncClient() as c:
        res = await discovery.discover_boards(db, c, sources=[github_lists.SOURCE])
    assert sorted(res.added) == ["bamboohr/acme", "smartrecruiters/Sr1"] and not res.rejected
    assert not any("/detail" in u or u.endswith("/postings/9") for u in stats["urls"])
