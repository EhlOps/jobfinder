import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from security_payloads import PRIVATE_URLS, SCRIPT_URLS, SQLI, XSS_TEXT

from jobfinder.ingest import http_safety
from jobfinder.models import Job, JobLink, Task
from jobfinder.services.resolver import resolve_link
from jobfinder.services.resolver.ladder import ResolveContext, SingleJob
from jobfinder.services.resolver.rungs.jsonld import (
    MAX_BLOCKS,
    JsonLdRung,
    find_job_posting,
    posting_from_ld,
)
from tests.fakes import fake_resolve_public, handle

FIX = Path(__file__).parent / "fixtures"
LD_JOB = (FIX / "jsonld_job.html").read_text()
LD_MALFORMED = (FIX / "jsonld_malformed.html").read_text()
PAGE = "https://careers.acmerobotics.com/jobs/4021"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(http_safety, "resolve_public", fake_resolve_public(refuse_private=True))


def page_client(web, body: str, seen: list | None = None) -> httpx.AsyncClient:
    def handler(request):
        if seen is not None:
            seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, html=body)

    handle(web, handler)
    return httpx.AsyncClient()


async def run(web, body: str, url: str = PAGE):
    async with page_client(web, body) as c:
        ctx = ResolveContext(url, c)
        return await JsonLdRung().run(ctx), ctx


def ld_page(*objs, raw: str = "") -> str:
    blocks = "".join(f'<script type="application/ld+json">{json.dumps(o)}</script>' for o in objs)
    return f"<html><head>{blocks}{raw}</head><body></body></html>"


async def test_graph_job_posting_becomes_a_single_job(web):
    hit, ctx = await run(web, LD_JOB)
    assert isinstance(hit, SingleJob)
    p = hit.posting
    assert p.source == "jsonld" and p.url == PAGE and p.company_name == "Acme Robotics"
    assert p.title == "Senior Robotics Engineer & Controls Lead"
    assert "Design motion control for our arm." in p.description_text and "- C++ and ROS" in p.description_text
    assert "<" not in p.description_text
    assert p.location == "Boston, MA, US; Austin, TX, US" and p.workplace_type == "remote"
    assert (p.salary_min, p.salary_max, p.salary_currency) == (150000, 190000, "USD")
    assert p.employment_type == "FULL_TIME" and p.seniority == "senior"
    assert p.posted_at == datetime(2026, 10, 1, tzinfo=UTC)
    assert not ctx.notes  # sameAs is the page's own registrable domain


async def test_malformed_blocks_are_skipped_and_wrappers_tolerated(web):
    hit, _ = await run(web, LD_MALFORMED)
    p = hit.posting
    assert p.title == "Warehouse Associate" and p.company_name == "Contoso Logistics" and p.location == "Reno, NV"
    assert "raw newline" in p.description_text
    # hourly baseSalary is ignored; the annual range in the text is used instead
    assert (p.salary_min, p.salary_max, p.salary_currency) == (40000, 48000, "USD")
    assert p.posted_at == datetime(2026, 9, 30, 8, 15, tzinfo=UTC)


def test_type_variants_and_top_level_arrays():
    assert find_job_posting(ld_page([{"@type": "Thing"}, {"@type": ["Thing", "JobPosting"], "title": "A"}]))["title"] == "A"
    assert find_job_posting(ld_page({"@type": "Organization"})) is None
    assert find_job_posting("<script type=application/ld+json>{\"@type\":\"JobPosting\",\"title\":\"B\"}</script>")["title"] == "B"
    escaped = '<script type="application/ld+json">{&quot;@type&quot;:&quot;JobPosting&quot;,&quot;title&quot;:&quot;C&quot;}</script>'
    assert find_job_posting(escaped)["title"] == "C"
    assert find_job_posting("<script type='application/ld+json'>{\"@type\":\"JobPosting\"") is None  # unclosed


def test_nested_cdata_and_partial_salary():
    nested = {"@type": "WebPage", "mainEntity": {"@type": "JobPosting", "title": "Nested"}}
    assert find_job_posting(ld_page(nested))["title"] == "Nested"
    listed = {"@type": "ItemList",
              "itemListElement": [{"@type": "ListItem", "item": {"@type": "JobPosting", "title": "I"}}]}
    assert find_job_posting(ld_page(listed))["title"] == "I"
    cdata = '<script type="application/ld+json"><![CDATA[{"@type":"JobPosting","title":"CD"}]]></script>'
    assert find_job_posting(cdata)["title"] == "CD"
    stacked = ('<script type="application/ld+json"><!--\n//<![CDATA[\n{"@type":"JobPosting","title":"S"}'
               '\n//]]>\n--></script>')
    assert find_job_posting(stacked)["title"] == "S"
    p = posting_from_ld({"@type": "JobPosting", "title": "Eng", "baseSalary": {
        "currency": "usd", "value": {"maxValue": 150000, "unitText": "YEAR"}}}, PAGE)
    assert (p.salary_min, p.salary_max, p.salary_currency) == (None, 150000, "USD")


async def test_valid_through_today_is_kept(web):
    today = datetime.now(UTC).date().isoformat()
    hit, _ = await run(web, ld_page({"@type": "JobPosting", "title": "Today", "validThrough": today}))
    assert hit.posting.title == "Today"


async def test_expired_posting_is_dropped(web):
    hit, ctx = await run(web, ld_page({"@type": "JobPosting", "title": "Old", "validThrough": "2020-01-01"}))
    assert hit is None and "expired" in ctx.notes[0]


async def test_aggregator_page_is_noted_not_fetched(web):
    seen: list[str] = []
    org = {"@type": "Organization", "name": "Acme", "sameAs": ["https://www.acme.com/"]}
    async with page_client(web, ld_page({"@type": "JobPosting", "title": "Eng", "hiringOrganization": org}), seen) as c:
        ctx = ResolveContext("https://www.jobaggregator.example/view/1", c)
        hit = await JsonLdRung().run(ctx)
    assert hit.posting.company_name == "Acme" and any("acme.com" in n for n in ctx.notes)
    assert seen == ["/robots.txt", "/view/1"]


async def test_no_json_ld_passes(web):
    hit, ctx = await run(web, "<html><body><h1>Careers</h1></body></html>")
    assert hit is None and not ctx.notes
    hit, _ = await run(web, ld_page({"@type": "JobPosting", "title": "<script>x</script>"}))
    assert hit is None  # no usable title


@pytest.mark.parametrize("xss", XSS_TEXT)
def test_hostile_fields_are_inert(xss):
    ld = {"@type": "JobPosting", "title": f"Engineer {xss}", "description": f"<p>Hi</p>{xss}",
          "hiringOrganization": {"name": f"{SQLI[0]} {xss}"}, "jobLocation": {"address": xss},
          "url": SCRIPT_URLS[0]}
    p = posting_from_ld(ld, PAGE)
    for field in (p.title, p.description_text, p.company_name, p.location):
        assert "<script" not in field and "<img" not in field and "<svg" not in field
    assert p.title.startswith("Engineer") and p.url == PAGE
    assert SQLI[0] in p.company_name  # stored as text through the ORM, never interpolated


@pytest.mark.parametrize("bad", SCRIPT_URLS)
def test_script_page_url_never_becomes_the_link(bad):
    assert posting_from_ld({"@type": "JobPosting", "title": "Eng"}, bad).url == ""


def test_json_ld_bombs_are_bounded():
    deep = '<script type="application/ld+json">' + "[" * 200_000 + "]" * 200_000 + "</script>"
    oversized = '<script type="application/ld+json">[' + ",".join(['{"@type":"Thing"}'] * 80_000) + "]</script>"
    wide = '<script type="application/ld+json">[' + ",".join(['{"@type":"Thing"}'] * 50_000) + "]</script>"
    many = '<script type="application/ld+json">{"@type":"Thing"}</script>' * (MAX_BLOCKS + 5)
    late = ld_page({"@type": "JobPosting", "title": "Late"})
    assert find_job_posting(deep + late) is not None  # a nesting bomb is skipped, not fatal
    assert find_job_posting(oversized + late) is not None  # a block over MAX_BLOCK_CHARS is not parsed
    assert find_job_posting(wide + late)["title"] == "Late"  # the node budget is per block
    wide_tail = wide.replace("]</script>", ',{"@type":"JobPosting","title":"Tail"}]</script>')
    assert find_job_posting(wide_tail) is None  # ...and runs out long before the last node
    assert find_job_posting(many + late) is None  # only the first MAX_BLOCKS blocks are read
    unclosed = '<script type="application/ld+json">{' * 50_000
    assert find_job_posting(unclosed) is None


@pytest.mark.parametrize("url", PRIVATE_URLS)
async def test_private_url_passes_without_fetching(url, web):
    seen: list[str] = []
    async with page_client(web, LD_JOB, seen) as c:
        ctx = ResolveContext(url, c)
        assert await JsonLdRung().run(ctx) is None
    assert seen == [] and ctx.page_error


async def test_json_ld_only_page_keeps_the_single_job(authed, maker, web):
    r = await authed.post("/api/job-links", json={"url": PAGE})
    assert r.status_code == 202, r.text
    async with maker() as db:
        [task] = (await db.scalars(sa.select(Task).where(Task.kind == "resolve_job_link"))).all()
        out = await resolve_link(db, task.payload["link_id"], client=page_client(web, LD_JOB))
        await db.commit()  # what the resolve_job_link task commits
        link = await db.get(JobLink, task.payload["link_id"])
        job = await db.get(Job, link.job_id)
    assert out["status"] == "unresolved" and out["job_id"] == job.id
    assert link.rung == "single_job" and job.source == "link" and job.url == PAGE
    assert job.title == "Senior Robotics Engineer & Controls Lead"
    body = (await authed.get(f"/api/job-links/{r.json()['id']}")).json()
    assert body["status"] == "unresolved" and [j["title"] for j in body["jobs"]] == [job.title]
