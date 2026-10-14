from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from security_payloads import PRIVATE_URLS, SCRIPT_URLS, SQLI
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.ingest import http_safety
from jobfinder.ingest.resolve import resolve_link
from jobfinder.ingest.resolve.ladder import BoardHit, ResolveContext
from jobfinder.ingest.resolve.rungs import RUNGS
from jobfinder.ingest.resolve.rungs.embedded import EmbeddedRung, page_boards, pick_board
from jobfinder.models import Company, Job, JobLink, Task

FIX = Path(__file__).parent / "fixtures"
GH_EMBED = (FIX / "careers_greenhouse_embed.html").read_text()
LEVER_LINKS = (FIX / "careers_lever_links.html").read_text()
PHENOM_JOB = (FIX / "phenom_job.html").read_text()
PUBLIC_IP = "93.184.216.34"
GH_JOBS = {"jobs": [
    {"id": 1, "title": "Robotics Engineer", "absolute_url": "https://boards.greenhouse.io/acmerobotics/jobs/1",
     "location": {"name": "Boston, MA"}},
    {"id": 2, "title": "Controls Engineer", "absolute_url": "https://boards.greenhouse.io/acmerobotics/jobs/2",
     "location": {"name": "Remote"}},
]}


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    async def fake(url):
        host = httpx.URL(url).host
        if host in ("localhost", "0.0.0.0") or host.startswith(("127.", "10.", "192.168.", "169.254.", "::1")):
            raise http_safety.FetchError("URL points to a non-public address")
        return host, PUBLIC_IP

    monkeypatch.setattr(http_safety, "resolve_public", fake)


@pytest.fixture
def maker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


def site(pages: dict[str, httpx.Response], seen: list | None = None):
    """Route by Host header (safe_fetch connects by IP) or URL host (ATS APIs); robots.txt is absent."""

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host") or request.url.host
        if seen is not None:
            seen.append(f"{host}{request.url.path}")
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        for key, resp in pages.items():
            if f"{host}{request.url.path}".startswith(key):
                return resp
        return httpx.Response(404, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def html(body: str) -> httpx.Response:
    return httpx.Response(200, html=body)


async def run(url: str, client: httpx.AsyncClient):
    async with client:
        ctx = ResolveContext(url, client)
        return await EmbeddedRung().run(ctx), ctx


def test_rung_order():
    assert [r.name for r in RUNGS][:3] == ["url_pattern", "embedded", "jsonld"]
    assert [r.order for r in RUNGS][:3] == [1.0, 2.0, 3.0]


def test_page_boards_finds_every_kind_of_reference():
    page = """
      <iframe src="https://boards.greenhouse.io/embed/job_board?for=gh1"></iframe>
      <script src="//job-boards.greenhouse.io/embed/job_board/js?for=gh2"></script>
      <script>Grnhse.Settings = {boardToken: "gh3"};</script>
      <script>window.leverJobsOptions = {accountName: 'lv1', includeCss: true};</script>
      <div data-url="https:\\/\\/jobs.lever.co\\/lv2\\/abc"></div>
      <iframe src="https://jobs.ashbyhq.com/ash1/embed?version=2"></iframe>
      <a href="https://acme.wd5.myworkdayjobs.com/en-US/External/job/Boston/Eng_R1">x</a>
      <script>fetch("https://acme2.wd1.myworkdayjobs.com/wday/cxs/acme2/Careers/jobs")</script>
      <script>fetch('https://boards-api.greenhouse.io/v1/boards/gh4/jobs?content=true')</script>
      <script>fetch('https://api.lever.co/v0/postings/lv3?mode=json')</script>
      <script>fetch('https://api.ashbyhq.com/posting-api/job-board/ash2')</script>
      <a href="https://jobs.lever.co/">Powered by Lever</a>
    """
    assert list(page_boards(page)) == [
        ("greenhouse", "gh1"), ("greenhouse", "gh2"), ("lever", "lv2"), ("ashby", "ash1"),
        ("workday", "acme.wd5.External"), ("workday", "acme2.wd1.Careers"), ("greenhouse", "gh4"),
        ("lever", "lv3"), ("ashby", "ash2"), ("greenhouse", "gh3"), ("lever", "lv1"),
    ]


def test_pick_board_prefers_the_pages_own_domain_then_the_most_referenced():
    counts = page_boards(GH_EMBED)
    assert counts[("greenhouse", "acmerobotics")] == 3 and counts[("lever", "partnerco")] == 2
    assert pick_board(counts, "https://www.acmerobotics.com/careers") == ("greenhouse", "acmerobotics")
    lever = page_boards(LEVER_LINKS)
    assert pick_board(lever, "https://www.nwtraders.com/careers") == ("lever", "northwind")
    # the slug that matches the domain wins even when it's referenced less
    assert pick_board(lever, "https://contoso.com/about") == ("greenhouse", "contoso")
    # a news article mentioning another company's board once is not that company's careers page
    assert pick_board(page_boards("<a href='https://jobs.lever.co/foo'>x</a>"), "https://technews.com/a") is None
    tie = page_boards(" ".join(["https://jobs.lever.co/aa"] * 2 + ["https://jobs.lever.co/bb"] * 2))
    assert pick_board(tie, "https://example.com/") is None
    assert list(page_boards("fetch('https://ACME.WD5.myworkdayjobs.com/wday/cxs/ACME/Ext/jobs')")) == [
        ("workday", "acme.wd5.Ext")]
    many = page_boards(" ".join(f"https://jobs.lever.co/co{i}" for i in range(8)))
    assert pick_board(many, "https://blog.example.com/startups-hiring") is None
    assert pick_board(many, "https://co3.com/") == ("lever", "co3")


async def test_greenhouse_iframe_page():
    hit, _ = await run("https://www.acmerobotics.com/careers",
                       site({"www.acmerobotics.com/careers": html(GH_EMBED)}))
    assert hit == BoardHit("greenhouse", "acmerobotics")


async def test_lever_link_list_ignores_script_urls():
    hit, _ = await run("https://www.nwtraders.com/careers", site({"www.nwtraders.com/careers": html(LEVER_LINKS)}))
    assert hit == BoardHit("lever", "northwind")


async def test_careers_link_redirecting_to_workday():
    pages = {
        "acme.com/careers": httpx.Response(302, headers={"location": "https://acme.wd5.myworkdayjobs.com/en-US/External"}),
        "acme.wd5.myworkdayjobs.com/en-US/External": html("<html>Workday</html>"),
    }
    hit, _ = await run("https://acme.com/careers", site(pages))
    assert hit == BoardHit("workday", "acme.wd5.External")


async def test_phenom_page_detected_from_html():
    hit, _ = await run("https://careers.mastercard.com/us/en/job/R-1",
                       site({"careers.mastercard.com/us/en/job/R-1": html(PHENOM_JOB)}))
    assert hit == BoardHit("phenom", "careers.mastercard.com__us__en")


async def test_page_without_boards_passes():
    hit, ctx = await run("https://acme.com/careers", site({"acme.com/careers": html("<a href='/jobs'>Jobs</a>")}))
    assert hit is None and not ctx.notes


@pytest.mark.parametrize("url", PRIVATE_URLS)
async def test_private_url_is_never_fetched(url):
    seen: list[str] = []
    hit, ctx = await run(url, site({"": html(GH_EMBED)}, seen))
    assert hit is None and ctx.page_error and seen == []


async def test_hostile_page_content_is_inert():
    junk = "".join(f'<a href="{u}">x</a>' for u in SCRIPT_URLS)
    junk += "".join(f'<a href="https://jobs.lever.co/{s}">x</a>' for s in SQLI)
    junk += '<script>Grnhse.Settings = {boardToken: "x\'; DROP TABLE companies;--"}</script>'
    junk += "<a href='https://boards.greenhouse.io/embed/job_board?for=javascript:alert(1)'>x</a>"
    hit, _ = await run("https://acme.com/careers", site({"acme.com/careers": html(junk)}))
    # SQLI slugs are cut at the first character a slug can't hold; nothing else survives
    assert hit is None or (hit.ats == "lever" and all(c.isalnum() or c in "._-" for c in hit.slug))
    assert not page_boards("".join(f'<a href="{u}">x</a>' for u in SCRIPT_URLS))


async def test_resolver_adds_the_embedded_board(authed, maker):
    r = await authed.post("/api/job-links", json={"url": "https://www.acmerobotics.com/careers"})
    assert r.status_code == 202, r.text
    pages = {
        "www.acmerobotics.com/careers": html(GH_EMBED),
        "boards-api.greenhouse.io/v1/boards/acmerobotics/jobs": httpx.Response(200, json=GH_JOBS),
    }
    async with maker() as db:
        [task] = (await db.scalars(sa.select(Task).where(Task.kind == "resolve_job_link"))).all()
        out = await resolve_link(db, task.payload["link_id"], client=site(pages))
        link = await db.get(JobLink, task.payload["link_id"])
        company = await db.get(Company, link.company_id)
        jobs = (await db.scalars(sa.select(Job).where(Job.company_id == company.id))).all()
    assert out["status"] == "resolved" and out["rung"] == "embedded" and out["added"], out
    assert link.rung == "embedded" and (company.ats, company.slug, company.origin) == ("greenhouse", "acmerobotics", "user")
    assert len(jobs) == 2
    body = (await authed.get(f"/api/job-links/{r.json()['id']}")).json()
    assert body["status"] == "resolved" and len(body["jobs"]) == 2
