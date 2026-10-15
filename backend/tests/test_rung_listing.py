import json
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from factories import make_user
from security_payloads import PRIVATE_URLS

from jobfinder.ingest import http_safety
from jobfinder.ingest.recipes import Recipe, run_recipe
from jobfinder.ingest.recipes.sniff import json_recipe_from_sample, pagination_for
from jobfinder.ingest.resolve import resolve_link
from jobfinder.ingest.resolve.ladder import RecipeHit, ResolveContext
from jobfinder.ingest.resolve.rungs import RUNGS
from jobfinder.ingest.resolve.rungs.listing import MAX_FETCHES, ListingRung, job_pattern
from jobfinder.ingest.service import ingest_company
from jobfinder.models import Company, Job, JobLink
from tests.fakes import fake_resolve_public

FIXTURES = Path(__file__).parent / "fixtures"
JOB_PAGE = (FIXTURES / "recipe_job_page.html").read_text()
LISTING = (FIXTURES / "recipe_listing.html").read_text()
API = json.loads((FIXTURES / "recipe_jobs_api.json").read_text())
HOST = "careers.acme.com"
PLAIN_JOB_PAGE = '<html><body><h1>Robotics Engineer</h1><a href="/careers">Careers</a></body></html>'


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(http_safety, "resolve_public", fake_resolve_public())


class Site:
    """A fake careers site: {path or path?query: response}. Records every request but robots.txt."""

    def __init__(self, routes: dict, robots: str | None = None):
        self.routes, self.robots, self.calls = routes, robots, []

    def handler(self, request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=self.robots) if self.robots else httpx.Response(404)
        host = request.headers["host"]
        self.calls.append(f"{host}{request.url.raw_path.decode()}")
        if host != HOST:
            return httpx.Response(200, json=API)  # would "work": it must never be asked
        query = request.url.query.decode()
        r = self.routes.get(f"{request.url.path}?{query}" if query else request.url.path)
        if r is None:
            return httpx.Response(404)
        if isinstance(r, httpx.Response):
            return r
        if isinstance(r, dict | list):
            return httpx.Response(200, json=r)
        ctype = "application/xml" if r.lstrip().startswith("<?xml") else "text/html; charset=utf-8"
        return httpx.Response(200, text=r, headers={"content-type": ctype})

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def json_site(**extra):
    return Site({"/jobs/4021": JOB_PAGE, "/api/v1/jobs?limit=2&offset=0": API,
                 "/api/v1/jobs?limit=2&offset=2": {"data": {"results": []}}, **extra})


async def sniff(site: Site, url=f"https://{HOST}/jobs/4021"):
    async with site.client() as c:
        ctx = ResolveContext(url, c)
        return await ListingRung().run(ctx), ctx


def test_rung_is_registered_at_order_4():
    assert any(r.name == "listing" and r.order == 4.0 for r in RUNGS)
    assert [r.order for r in RUNGS] == sorted(r.order for r in RUNGS)


@pytest.mark.parametrize(("url", "pattern"), [
    ("https://x.com/jobs/4021", "/jobs/"),
    ("https://x.com/careers/job/123-eng/apply", "/careers/job/"),
    ("https://x.com/en/jobs/eng-123/", "/en/jobs/"),
    ("https://x.com/job?id=77&lang=en", "/job?id="),
    ("https://x.com/4021", None),
    ("https://x.com/", None),
    ("https://x.com/jobs/4021/senior-engineer", "/jobs/"),  # the id segment, not the slug, varies
    ("https://x.com/a'b/4021", None),  # never an unsafe selector value
    ("https://x.com/a]b/4021", None),
])
def test_job_pattern(url, pattern):
    assert job_pattern(url) == pattern


def test_json_recipe_from_sample_detects_fields():
    r = json_recipe_from_sample(f"https://{HOST}/api/v1/jobs?limit=2&offset=0", "GET", API,
                                pasted_url=f"https://{HOST}/jobs/4021")
    assert isinstance(r, Recipe) and r.kind == "json_api" and r.items == "$.data.results[*]"
    f = r.fields
    assert (f.external_id, f.title, f.location, f.posted_at, f.description) == (
        "$.jobId", "$.jobTitle", "$.locationName", "$.datePosted", "$.description")
    assert f.url == f"https://{HOST}/jobs/{{external_id}}"  # templated from the pasted URL
    assert (r.pagination.kind, r.pagination.param, r.pagination.step) == ("offset", "offset", 2)


def test_json_recipe_from_sample_shapes():
    top = [{"id": 1, "name": "A", "absolute_url": "https://x.com/j/1", "updated_at": "2026-10-01"},
           {"id": 2, "name": "B", "absolute_url": "https://x.com/j/2"}]
    r = json_recipe_from_sample("https://x.com/api/jobs", "GET", top)
    assert r.items == "$[*]" and r.fields.url == "$.absolute_url" and r.fields.posted_at == "$.updated_at"
    nested = {"groups": [{"jobs": [{"reqId": "R1", "title": "A", "applyUrl": "/a"}]},
                         {"jobs": [{"reqId": "R2", "title": "B", "applyUrl": "/b"}]}],
              "offices": [{"id": 1, "name": "Boston"}]}
    r = json_recipe_from_sample("https://x.com/api/jobs", "POST", nested, {"page": 1, "q": ""})
    assert r.items == "$.groups[*].jobs[*]" and r.fields.external_id == "$.reqId"
    assert r.list.method == "POST" and r.list.json_body == {"page": 1, "q": ""}
    assert (r.pagination.kind, r.pagination.param, r.pagination.start) == ("page", "page", 1)
    urls = [{"url": "https://x.com/job/1/", "title": "A"}, {"url": "https://x.com/job/2/", "title": "B"}]
    r = json_recipe_from_sample("https://x.com/api/jobs/", "GET", urls)  # no id: the URL is the id
    assert r.fields.external_id == r.fields.url == "$.url"
    assert json_recipe_from_sample("https://x.com/api", "GET", {"a": [{"x": 1}]}) is None
    assert json_recipe_from_sample("https://x.com/api", "GET", "not json") is None
    # no URL in the items and the pasted URL doesn't name one of them: can't link the jobs
    assert json_recipe_from_sample("https://x.com/api", "GET", [{"id": 1234, "title": "A"}],
                                   pasted_url="https://x.com/jobs/9999") is None
    odd = [{"id": 1, "title": "A", 'we"ird': 1}]
    assert json_recipe_from_sample("https://x.com/api", "GET", odd, pasted_url="https://x.com/j/1") is None


def test_url_template_from_pasted_link():
    body = [{"id": 12345, "title": "A"}, {"id": 12346, "title": "B"}]
    r = json_recipe_from_sample("https://x.com/api/jobs", "GET", body,
                                pasted_url="https://x.com/jobs/12345/eng-123456")
    assert r.fields.url == "https://x.com/jobs/{external_id}/"  # not inside 'eng-123456', slug dropped
    r = json_recipe_from_sample("https://x.com/api/jobs", "GET", body, pasted_url="https://x.com/job?id=12345&l=en")
    assert r.fields.url == "https://x.com/job?id={external_id}&l=en"


def test_pagination_for():
    assert pagination_for("https://x/api?page=0")["kind"] == "page"
    assert pagination_for("https://x/api?page=3")["start"] == 1  # always from the first page
    assert pagination_for("https://x/api?offset=40&limit=20")["start"] == 0
    assert pagination_for("https://x/api?start=0&rows=25") == {
        "kind": "offset", "param": "start", "start": 0, "step": 25}
    assert pagination_for("https://x/api?offset=0") == {"kind": "none"}  # no page size: can't step
    assert pagination_for("https://x/api", {"from": 0, "size": 50})["step"] == 50


async def test_json_endpoint_in_page_becomes_a_validated_recipe():
    site = json_site()
    hit, ctx = await sniff(site)
    assert isinstance(hit, RecipeHit), ctx.notes
    assert hit.domain == "acme.com" and hit.name == "Acme Robotics" and len(hit.postings) == 2
    assert hit.recipe.kind == "json_api" and hit.recipe.list.url.endswith("/api/v1/jobs?limit=2&offset=0")
    prov = hit.recipe.provenance
    assert prov["induced_by"] == "listing_sniff" and prov["validated_with"] == ctx.url and prov["induced_at"]
    p = {j.title: j for j in hit.postings}["Robotics Engineer"]
    assert p.url == f"https://{HOST}/jobs/4021" and p.external_id == "acme.com:4021"
    # graphql, static assets, other sites and private addresses were never requested
    assert all(c.startswith(HOST) for c in site.calls), site.calls
    assert not any("graphql" in c or c.endswith((".js", ".css", ".png")) for c in site.calls)


async def test_listing_page_becomes_an_html_recipe():
    site = Site({"/jobs/4021-robotics-engineer": PLAIN_JOB_PAGE, "/jobs": LISTING})
    hit, ctx = await sniff(site, f"https://{HOST}/jobs/4021-robotics-engineer")
    assert isinstance(hit, RecipeHit), ctx.notes
    r = hit.recipe
    assert r.kind == "html" and r.items == "a.job-title[href*='/jobs/']" and r.list.url == f"https://{HOST}/jobs"
    assert r.fields.title == ":scope" and r.fields.url == "@href"
    # 'jobs/4023-…' is a relative href without '/jobs/' in it: the anchor selector (rightly) skips it
    assert {p.title for p in hit.postings} == {"Robotics Engineer", "Controls Engineer"}
    async with site.client() as c:  # the stored recipe runs on its own
        again = await run_recipe(c, "acme.com", "Acme", r.model_dump(mode="json"))
    assert len(again) == 2


async def test_rejected_candidates_are_noted_and_ladder_moves_on():
    other = {"data": {"results": [{"jobId": "9001", "jobTitle": "Chef", "url": "/jobs/9001"},
                                  {"jobId": "9002", "jobTitle": "Cook", "url": "/jobs/9002"}]}}
    site = Site({"/jobs/4021-robotics-engineer": JOB_PAGE, "/api/v1/jobs?limit=2&offset=0": other,
                 "/api/v1/jobs?limit=2&offset=2": [], "/jobs": LISTING})
    hit, ctx = await sniff(site, f"https://{HOST}/jobs/4021-robotics-engineer")
    assert hit is not None and hit.recipe.kind == "html"
    assert any("don't include the pasted job" in n for n in ctx.notes), ctx.notes


async def test_sitemap_names_the_listing_page():
    xml = (f'<?xml version="1.0"?><urlset><url><loc>https://{HOST}/en/open-positions</loc></url>'
           f'<url><loc>https://{HOST}/jobs/4021-robotics-engineer</loc></url>'
           f'<url><loc>https://{HOST}/jobs/4022-controls-engineer</loc></url>'
           f'<url><loc>https://evil.example/en/open-positions</loc></url></urlset>')
    site = Site({"/jobs/4021-robotics-engineer": "<html><body>Job</body></html>", "/sm.xml": xml,
                 "/en/open-positions": LISTING},
                robots=f"User-agent: *\nAllow: /\nSitemap: https://{HOST}/sm.xml\n")
    hit, ctx = await sniff(site, f"https://{HOST}/jobs/4021-robotics-engineer")
    assert hit is not None and hit.recipe.list.url == f"https://{HOST}/en/open-positions", ctx.notes
    assert any("sitemap lists 2 URLs like /jobs/" in n for n in ctx.notes)
    assert all(c.startswith(HOST) for c in site.calls)


async def test_listing_page_named_in_js_is_still_tried_as_a_page():
    page = '<html><body><script>var nav = {"careers": "/careers"};</script></body></html>'
    site = Site({"/jobs/4021-robotics-engineer": page, "/careers": LISTING})
    hit, ctx = await sniff(site, f"https://{HOST}/jobs/4021-robotics-engineer")
    assert hit is not None and hit.recipe.list.url == f"https://{HOST}/careers", ctx.notes


async def test_listing_selector_skips_nav_and_filter_links():
    lis = "".join(f'<li><h3><a href="/jobs/{i}/">Engineer {i}</a></h3>'
                  f'<a href="/jobs/location/boston/">Boston</a> <a href="/jobs/{i}/apply">Apply</a></li>'
                  for i in (4021, 4022, 4023))
    listing = (f'<nav><a href="/jobs/">Jobs</a><a href="/jobs/category/eng/">Eng</a></nav><ul>{lis}</ul>'
               f'<a href="/jobs/?page=2">Next</a>')
    site = Site({"/jobs/4021/": PLAIN_JOB_PAGE, "/jobs": listing, "/jobs?page=1": listing,
                 "/jobs?page=2": "<p>end</p>"})
    hit, ctx = await sniff(site, f"https://{HOST}/jobs/4021/")
    assert hit is not None, ctx.notes
    assert hit.recipe.items == "h3 > a[href*='/jobs/']"
    assert (hit.recipe.pagination.kind, hit.recipe.pagination.param) == ("page", "page")
    assert [p.title for p in hit.postings] == ["Engineer 4021", "Engineer 4022", "Engineer 4023"]


async def test_fetch_budget_is_capped():
    refs = "".join(f'<script>var u{i} = "/api/jobs/{i}.json";</script>' for i in range(20))
    links = "".join(f'<a href="/x{i}/careers">c</a>' for i in range(20))
    site = Site({"/jobs/4021": f"<html><body>{refs}{links}</body></html>"})
    hit, ctx = await sniff(site)
    assert hit is None
    assert len(site.calls) - 1 <= MAX_FETCHES, site.calls  # minus the pasted page itself
    assert ctx.notes


async def test_robots_disallowed_candidates_are_skipped():
    site = json_site()
    site.robots = "User-agent: *\nDisallow: /api/\n"
    hit, ctx = await sniff(site)
    assert not any("/api/" in c for c in site.calls)
    assert any("robots" in n for n in ctx.notes)
    assert hit is None or hit.recipe.kind == "html"


@pytest.mark.parametrize("bad", PRIVATE_URLS + ["https://evil.example/api/jobs.json",
                                                "https://acme.com.evil.example/api/jobs"])
async def test_foreign_and_private_endpoints_never_fetched(bad):
    page = f'<html><body><script>var api = "{bad}";</script><a href="{bad}careers">x</a></body></html>'
    site = Site({"/jobs/4021": page})
    await sniff(site)
    assert all(c.startswith(HOST) for c in site.calls), site.calls


async def test_huge_endpoint_is_capped_and_noted():
    big = httpx.Response(200, content=b"[" + b'{"id":1,"title":"x"},' * 200_000 + b"{}]",
                         headers={"content-type": "application/json"})
    site = json_site(**{"/api/v1/jobs?limit=2&offset=0": big})
    hit, ctx = await sniff(site)
    assert hit is None and any("too large" in n for n in ctx.notes)


async def test_unfetchable_page_still_tries_listing_pages():
    site = Site({"/jobs": LISTING})  # the job page itself 404s
    hit, _ = await sniff(site, f"https://{HOST}/jobs/4021-robotics-engineer")
    assert hit is not None and hit.recipe.kind == "html"
    assert hit.name is None


# ── through the resolver and ingest ───────────────────────────────────────
async def test_paste_resolves_to_recipe_board_and_goes_active(maker):
    site = json_site()
    async with maker() as db, site.client() as c:
        u = await make_user(db)
        link = JobLink(user_id=u.id, url=f"https://{HOST}/jobs/4021", status="resolving")
        db.add(link)
        await db.commit()
        out = await resolve_link(db, link.id, client=c)
        assert out["status"] == "resolved" and out["rung"] == "listing", out
        company = await db.get(Company, out["company_id"])
        assert company.ats == "recipe" and company.slug == "acme.com" and company.name == "Acme Robotics"
        assert company.recipe["provenance"]["induced_by"] == "listing_sniff"
        assert company.recipe_state == "active"  # the immediate first fetch was healthy
        jobs = (await db.scalars(sa.select(Job).where(Job.company_id == company.id))).all()
        assert len(jobs) == 2
        await ingest_company(db, c, company)
        await db.refresh(company)
        assert company.recipe_state == "active" and company.recipe_health["baseline_yield"] == 2
