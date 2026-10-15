"""Rung 4.5 without a browser: captured JSON -> json_api recipe, the rendered-DOM fallback, and the off switch."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from jobfinder import renderer
from jobfinder.config import get_settings
from jobfinder.ingest import http_safety
from jobfinder.ingest.recipes import RecipeError, run_recipe, validate_recipe
from jobfinder.ingest.recipes.rendered import item_id
from jobfinder.ingest.recipes.sniff import explain_json_sample, pagination_for
from jobfinder.ingest.resolve.ladder import RecipeHit, ResolveContext
from jobfinder.ingest.resolve.rungs import RUNGS
from jobfinder.ingest.resolve.rungs.rendered import (
    RenderedRung,
    job_pattern,
    listing_url,
    rendered_html_recipe,
)
from jobfinder.models import Company, Task
from jobfinder.scheduling import queue

FIX = Path(__file__).parent / "fixtures" / "renderer"
POST_URL = "https://careers.acme.com/us/en/job/R-10234/senior-robotics-engineer"
DOM_URL = "https://www.widgetco.io/careers/positions/5502"


def load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(renderer, "POLL_S", 0.01)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "renderer_enabled", True)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    async def fake(url):
        return httpx.URL(url).host, "93.184.216.34"

    monkeypatch.setattr(http_safety, "resolve_public", fake)


def search_api(calls):
    page0 = load("search_page0.json")

    def handler(request: httpx.Request):
        if request.url.path == "/widgets" and request.method == "POST":
            body = json.loads(request.content)
            calls.append(body)
            return httpx.Response(200, json=page0 if body.get("from") == 0 else {"data": {"jobs": []}})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class FakeRenderer:
    """Plays the renderer container: completes render_* tasks with canned results and records them."""

    def __init__(self, maker, results):
        self.maker, self.results, self.seen = maker, results, []

    async def loop(self):
        while True:
            async with self.maker() as db:
                t = await queue.claim(db, kind_prefix=queue.RENDER_PREFIX)
                if t:
                    self.seen.append((t.kind, t.payload))
                    await queue.complete(db, t, self.results[t.kind])
                    continue
            await asyncio.sleep(0.01)

    async def __aenter__(self):
        self.task = asyncio.create_task(self.loop())
        return self

    async def __aexit__(self, *exc):
        self.task.cancel()


def test_rung_is_registered_between_listing_sniff_and_llm():
    r = next(r for r in RUNGS if r.name == "rendered")
    assert r.order == 4.5


def test_renderer_is_off_by_default():
    assert type(get_settings()).model_fields["renderer_enabled"].default is False


async def test_disabled_rung_passes_without_enqueueing(maker):
    assert get_settings().renderer_enabled is False
    async with httpx.AsyncClient() as client:
        assert await RenderedRung().run(ResolveContext(POST_URL, client)) is None
    async with maker() as db:
        assert (await db.execute(Task.__table__.select())).first() is None


async def test_rendered_html_runner_refuses_when_disabled(maker):
    recipe = {"kind": "rendered_html", "list": {"url": "https://www.widgetco.io/careers/"},
              "items": "a[href*='/careers/positions/']",
              "fields": {"external_id": "@href", "title": ":scope", "url": "@href"}}
    async with httpx.AsyncClient() as client:
        with pytest.raises(RecipeError, match="switched off"):
            await run_recipe(client, "widgetco.io", "Widgetco", recipe)


def test_captured_post_search_becomes_a_recipe_candidate():
    """The capture's JSON goes through the shared sniff helper (U9's), with the captured POST body."""
    cap = load("capture_post_search.json")
    resp = cap["json_responses"][1]
    body = json.loads(resp["request_body"])
    recipe, why = explain_json_sample(resp["url"], "POST", json.loads(resp["body_preview"]), body,
                                      pasted_url=POST_URL)
    assert why == "" and recipe.list.method == "POST" and recipe.list.json_body == body
    assert recipe.items == "$.data.jobs[*]"
    assert (recipe.fields.external_id, recipe.fields.url, recipe.fields.description) == (
        "$.jobId", "$.applyUrl", "$.descriptionTeaser")
    assert (recipe.pagination.kind, recipe.pagination.param, recipe.pagination.step) == ("offset", "from", 3)
    for other in (cap["json_responses"][0], cap["json_responses"][2]):
        assert explain_json_sample(other["url"], "GET", json.loads(other["body_preview"]),
                                   pasted_url=POST_URL)[0] is None


def test_offset_without_size_steps_by_the_sample_length():
    """Eightfold-style `start=0` with no size parameter (the Microsoft careers capture)."""
    assert pagination_for("https://x.com/api/search?start=0", page_len=10) == {
        "kind": "offset", "param": "start", "start": 0, "step": 10}
    assert pagination_for("https://x.com/api/search?start=0", page_len=2) == {"kind": "none"}
    assert pagination_for("https://x.com/api/search?start=0") == {"kind": "none"}


@pytest.mark.real_commit
async def test_captured_json_becomes_validated_recipe(maker, enabled):
    calls = []
    async with FakeRenderer(maker, {"render_capture": load("capture_post_search.json")}) as fake, \
            search_api(calls) as client:
        hit = await RenderedRung().run(ResolveContext(POST_URL, client))
    assert isinstance(hit, RecipeHit) and hit.domain == "acme.com"
    assert hit.recipe.kind == "json_api" and hit.recipe.list.method == "POST"
    assert hit.recipe.provenance["induced_by"] == "rendered"
    assert [p.title for p in hit.postings][:1] == ["Senior Robotics Engineer"] and len(hit.postings) == 3
    assert hit.postings[0].external_id == "acme.com:R-10234" and hit.postings[0].posted_at.tzinfo
    assert fake.seen == [("render_capture", {"url": POST_URL})]
    assert calls[0]["from"] == 0 and calls[0]["ddoKey"] == "refineSearch"  # the captured body, replayed


@pytest.mark.real_commit
async def test_recipe_that_misses_the_pasted_job_is_not_used(maker, enabled):
    other = "https://careers.acme.com/us/en/job/R-99999/not-in-the-results"
    async with FakeRenderer(maker, {"render_capture": load("capture_post_search.json")}), \
            search_api([]) as client:
        ctx = ResolveContext(other, client)
        assert await RenderedRung().run(ctx) is None
    assert any("don't include the pasted job" in n for n in ctx.notes)
    # the page's own JSON calls are left for the LLM rung (same-site GETs only)
    assert "https://careers.acme.com/widgets" in ctx.json_tried
    assert [u for u, _ in ctx.json_samples] == ["https://careers.acme.com/widgets/config"]


@pytest.mark.real_commit
async def test_truncated_capture_is_refetched_safely(maker, enabled):
    cap = load("capture_post_search.json")
    search = cap["json_responses"][1]
    search.update(body_preview=search["body_preview"][:50], truncated=True)
    calls = []
    async with FakeRenderer(maker, {"render_capture": cap}), search_api(calls) as client:
        hit = await RenderedRung().run(ResolveContext(POST_URL, client))
    assert isinstance(hit, RecipeHit) and len(calls) >= 2  # one re-fetch for the sample, then the run


async def test_renderer_failure_is_noted(maker, enabled, monkeypatch):
    monkeypatch.setattr("jobfinder.ingest.resolve.rungs.rendered.CAPTURE_WAIT_S", 0.05)
    async with httpx.AsyncClient() as client:
        ctx = ResolveContext(POST_URL, client)
        assert await RenderedRung().run(ctx) is None
    assert any("did not answer" in n for n in ctx.notes)


def test_dom_recipe_from_repeated_job_links():
    cap = load("capture_dom_only.json")
    assert job_pattern(DOM_URL) == "/careers/positions/"
    assert job_pattern("https://acme.com/careers/job/123/apply") == "/careers/job/"
    assert job_pattern("https://acme.com/job") is None
    assert job_pattern(POST_URL) == "/us/en/job/"  # cut at the id, not before the slug
    assert job_pattern("https://acme.com/jobs/senior-welder") == "/jobs/"
    assert listing_url(DOM_URL) == "https://www.widgetco.io/careers/positions"
    recipe, why = rendered_html_recipe(cap["html"], cap["final_url"], DOM_URL)
    assert why == "" and recipe["kind"] == "rendered_html"
    assert recipe["items"] == "a[href*='/careers/positions/']:not([href$='/careers/positions/'])" and recipe["list"]["url"] == cap["final_url"]
    none, why = rendered_html_recipe("<html><a href='/careers/positions/1'>one</a></html>", cap["final_url"],
                                     DOM_URL)
    assert none is None and "fewer than" in why


def dom_items():
    return {"final_url": "https://www.widgetco.io/careers/", "items": [
        {"external_id": f"https://www.widgetco.io/careers/positions/{n}", "title": t,
         "url": f"https://www.widgetco.io/careers/positions/{n}"}
        for n, t in ((5501, "Platform Engineer"), (5502, "Data Engineer\nRemote"), (5503, "Product Designer"))
    ] + [{"external_id": "javascript:alert(1)", "title": "x", "url": "javascript:alert(1)"}]}


@pytest.mark.real_commit
async def test_dom_only_site_gets_a_rendered_html_recipe(maker, enabled):
    results = {"render_capture": load("capture_dom_only.json"), "render_list": dom_items()}
    async with FakeRenderer(maker, results) as fake, httpx.AsyncClient() as client:
        hit = await RenderedRung().run(ResolveContext(DOM_URL, client))
    assert isinstance(hit, RecipeHit) and hit.recipe.kind == "rendered_html" and hit.domain == "widgetco.io"
    # the job page, then the listing page guessed from its URL; the DOM recipe comes from the listing page
    assert fake.seen[:2] == [("render_capture", {"url": DOM_URL}),
                             ("render_capture", {"url": "https://www.widgetco.io/careers/positions"})]
    assert [k for k, _ in fake.seen] == ["render_capture", "render_capture", "render_list"]
    by_title = {p.title: p for p in hit.postings}
    assert by_title["Data Engineer"].external_id == "widgetco.io:/careers/positions/5502"
    assert by_title["x"].url == ""  # javascript: never becomes a link
    ok, _ = validate_recipe(hit.postings, DOM_URL)
    assert ok


@pytest.mark.real_commit
async def test_rendered_html_boards_are_capped(maker, enabled, monkeypatch):
    monkeypatch.setattr(get_settings(), "renderer_max_rendered_boards", 1)
    async with maker() as db:
        db.add(Company(name="Old", ats="recipe", slug="old.example", origin="user",
                       recipe={"kind": "rendered_html"}))
        await db.commit()
    results = {"render_capture": load("capture_dom_only.json"), "render_list": dom_items()}
    async with FakeRenderer(maker, results) as fake, httpx.AsyncClient() as client:
        ctx = ResolveContext(DOM_URL, client)
        assert await RenderedRung().run(ctx) is None
    assert {k for k, _ in fake.seen} == {"render_capture"} and any("cap of 1" in n for n in ctx.notes)


def test_item_ids_from_links():
    base = "https://www.widgetco.io/careers/"
    assert item_id("/careers/positions/5501/", base) == "/careers/positions/5501"
    assert item_id("https://www.widgetco.io/job?id=7", base) == "/job?id=7"
    assert item_id("R-1234", base) == "R-1234"


async def test_renderer_not_running_stops_the_rung_quickly(maker, enabled, monkeypatch):
    monkeypatch.setattr("jobfinder.ingest.resolve.rungs.rendered.CAPTURE_WAIT_S", 0.05)
    async with httpx.AsyncClient() as client:
        ctx = ResolveContext(DOM_URL, client)
        assert await RenderedRung().run(ctx) is None
    async with maker() as db:
        rows = (await db.execute(Task.__table__.select())).all()
    assert len(rows) == 1  # no second capture once the renderer is known to be absent


async def test_renderer_off_skips_board_without_counting_a_failure(maker):
    from jobfinder.ingest.service import ingest_company

    async with maker() as db:
        c = Company(name="Widgetco", ats="recipe", slug="widgetco.io", origin="user", enabled=True,
                    recipe={"kind": "rendered_html", "list": {"url": "https://www.widgetco.io/careers/"},
                            "items": "a", "fields": {"external_id": "@href", "title": ":scope", "url": "@href"}})
        db.add(c)
        await db.commit()
        async with httpx.AsyncClient() as client:
            stats = await ingest_company(db, client, c)
        await db.refresh(c)
    assert stats.error.startswith("skipped:") and "switched off" in stats.error
    assert not c.consecutive_failures and c.enabled


def test_similar_jobs_endpoints_are_tried_last():
    from jobfinder.ingest.resolve.rungs.rendered import _candidate_order

    rs = [{"url": "https://x.com/api/similar_positions?id=1", "size": 9000},
          {"url": "https://x.com/api/search?start=0", "size": 5000},
          {"url": "https://x.com/api/branding", "size": 7000}]
    assert [r["url"].split("/api/")[1][:6] for r in sorted(rs, key=_candidate_order)] == [
        "brandi", "search", "simila"]
