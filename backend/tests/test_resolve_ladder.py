import httpx
import pytest
import sqlalchemy as sa
from factories import make_user
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.ingest import http_safety, resolve
from jobfinder.ingest.base import JobPosting
from jobfinder.ingest.recipes import Recipe
from jobfinder.ingest.resolve import registrable_domain, resolve_link
from jobfinder.ingest.resolve.ladder import BoardHit, RecipeHit, ResolveContext, SingleJob
from jobfinder.ingest.resolve.rungs import RUNGS
from jobfinder.ingest.service import ingest_company
from jobfinder.ingest.sources.detect import HTML_DETECTORS, detect_board
from jobfinder.models import Company, Job, JobLink
from tests.fakes import fake_resolve_public

URL = "https://careers.acme.com/jobs/4021"
API = {"jobs": [{"id": 4021, "title": "Robotics Engineer"}, {"id": 4022, "title": "Controls Engineer"}]}
RECIPE = {
    "kind": "json_api", "list": {"url": "https://careers.acme.com/api/jobs"}, "items": "$.jobs[*]",
    "fields": {"external_id": "$.id", "title": "$.title", "url": "https://careers.acme.com/jobs/{external_id}"},
}


@pytest.fixture
def maker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(http_safety, "resolve_public", fake_resolve_public())


class Fixed:
    def __init__(self, name, order, hit=None, boom=False):
        self.name, self.order, self.hit, self.boom, self.calls = name, order, hit, boom, 0

    async def run(self, ctx):
        self.calls += 1
        if self.boom:
            raise RuntimeError("rung bug")
        return self.hit


def api_client():
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=API)))


async def make_link(db, url=URL):
    u = await make_user(db)
    link = JobLink(user_id=u.id, url=url, status="resolving")
    db.add(link)
    await db.commit()
    return link


def test_rungs_are_ordered_and_start_with_url_pattern():
    assert RUNGS[0].name == "url_pattern"
    assert [r.order for r in RUNGS] == sorted(r.order for r in RUNGS)


def test_registrable_domain():
    assert registrable_domain("https://careers.acme.com/x") == "acme.com"
    assert registrable_domain("https://jobs.acme.co.uk/") == "acme.co.uk"
    assert registrable_domain("https://acme.io") == "acme.io"


async def test_recipe_hit_becomes_a_recipe_board(maker, monkeypatch):
    posting = JobPosting(source="recipe", external_id="acme.com:4021", company_name="Acme", title="R", url=URL)
    hit = RecipeHit(Recipe.model_validate(RECIPE), domain="acme.com", name="Acme Robotics", postings=[posting])
    broken, passes, wins = Fixed("broken", 2, boom=True), Fixed("passes", 3), Fixed("wins", 4, hit)
    never = Fixed("never", 9, BoardHit("greenhouse", "acme"))
    monkeypatch.setattr(resolve, "RUNGS", [broken, passes, wins, never])
    async with maker() as db, api_client() as c:
        link = await make_link(db)
        out = await resolve_link(db, link.id, client=c)
        await db.refresh(link)
        company = await db.get(Company, link.company_id)
        jobs = (await db.scalars(sa.select(Job).where(Job.company_id == company.id))).all()
    assert out["status"] == "resolved" and out["rung"] == "wins" and out["new"] == 2
    assert never.calls == 0 and broken.calls == passes.calls == 1
    assert link.rung == "wins" and link.ats == "recipe" and link.slug == "acme.com"
    assert company.ats == "recipe" and company.origin == "user" and company.name == "Acme Robotics"
    assert company.recipe["kind"] == "json_api" and company.recipe_state == "active"  # the first fetch was healthy
    assert {j.external_id for j in jobs} == {"acme.com:4021", "acme.com:4022"} and all(j.source == "recipe" for j in jobs)


async def test_single_job_is_kept_when_no_board(maker, monkeypatch):
    posting = JobPosting(source="jsonld", external_id="x", company_name="Acme", title="Robotics Engineer", url="")
    monkeypatch.setattr(resolve, "RUNGS", [Fixed("jsonld", 3, SingleJob(posting)), Fixed("later", 4)])
    async with maker() as db:
        link = await make_link(db)
        out = await resolve_link(db, link.id, client=api_client())
        await db.refresh(link)
        job = await db.get(Job, link.job_id)
    assert out["status"] == "unresolved" and out["job_id"] == job.id
    assert link.rung == "single_job" and "saved this job" in link.error
    assert job.source == "link" and job.company_id is None and job.url == URL and job.title == "Robotics Engineer"


async def test_failed_board_falls_through_to_next_rung(maker, monkeypatch):
    posting = JobPosting(source="recipe", external_id="acme.com:4021", company_name="Acme", title="R", url=URL)
    bad = Fixed("bad", 1, BoardHit("greenhouse", "nope"))
    good = Fixed("good", 2, RecipeHit(Recipe.model_validate(RECIPE), domain="acme.com", postings=[posting]))
    monkeypatch.setattr(resolve, "RUNGS", [bad, good])

    def handler(request):
        if "greenhouse" in request.url.host or request.headers.get("host", "").startswith("boards-api"):
            return httpx.Response(404, json={})
        return httpx.Response(200, json=API)

    async with maker() as db, httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        link = await make_link(db)
        out = await resolve_link(db, link.id, client=c)
    assert out["status"] == "resolved" and out["rung"] == "good"


async def test_board_error_reported_when_nothing_else_works(maker, monkeypatch):
    monkeypatch.setattr(resolve, "RUNGS", [Fixed("bad", 1, BoardHit("greenhouse", "nope"))])
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404, json={})))
    async with maker() as db:
        link = await make_link(db)
        await resolve_link(db, link.id, client=client)
        await db.refresh(link)
    assert link.status == "unresolved" and "couldn't be read" in link.error and "404" in link.error


async def test_context_fetches_page_once_and_notes_failures(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, html="<html>hi</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        ctx = ResolveContext(URL, c)
        assert "hi" in (await ctx.page()).text and (await ctx.page()) is not None
        assert calls.count("/jobs/4021") == 1
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as c:
        ctx = ResolveContext(URL, c)
        assert await ctx.page() is None and ctx.page_error and ctx.notes


async def test_unknown_ats_is_a_board_failure_not_a_crash(maker):
    async with maker() as db:
        c = Company(name="Gone", ats="no-such-ats", slug="gone")
        db.add(c)
        await db.commit()
        stats = await ingest_company(db, None, c)
        await db.refresh(c)
    assert "unknown ATS" in stats.error and c.consecutive_failures == 1 and "unknown ATS" in c.last_error


async def test_recipe_board_ingests_through_run_recipe(maker):
    async with maker() as db, api_client() as client:
        c = Company(name="Acme", ats="recipe", slug="acme.com", recipe=RECIPE, origin="user")
        db.add(c)
        await db.commit()
        stats = await ingest_company(db, client, c)
        broken = Company(name="Broken", ats="recipe", slug="broken.com", recipe={"kind": "json_api"})
        db.add(broken)
        await db.commit()
        bad = await ingest_company(db, client, broken)
    assert stats.fetched == 2 and stats.new == 2 and stats.error is None
    assert bad.error and "invalid recipe" in bad.error


def test_detect_board_skips_broken_detectors(monkeypatch):
    def boom(url, html):
        raise ValueError("bad detector")

    monkeypatch.setattr("jobfinder.ingest.sources.detect.HTML_DETECTORS",
                        [boom, lambda u, h: ("phenom", "x") if "phenompeople" in h else None])
    assert detect_board("https://careers.x.com", "<script src='//cdn.phenompeople.com'>") == ("phenom", "x")
    assert detect_board("https://careers.x.com", "<html/>") is None
    assert isinstance(HTML_DETECTORS, list)
