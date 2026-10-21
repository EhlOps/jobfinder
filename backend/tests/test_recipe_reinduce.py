"""Self-heal: a recipe board whose health goes degraded is queued for re-induction by ingest_company, and the
reinduce_recipe handler asks the LLM for a new recipe (at most REINDUCE_CAP times per 7 days per domain)."""
from datetime import UTC, datetime

import httpx
import pytest
import sqlalchemy as sa

from jobfinder.core.config import get_settings
from jobfinder.integrations import http_safety
from jobfinder.integrations.claude.claude_code import AIError
from jobfinder.integrations.claude.tasks import AITasks
from jobfinder.integrations.recipes import health
from jobfinder.models import AICall, Company, Job, Task
from jobfinder.scheduling import handlers
from jobfinder.services.ingestion import ingest_company
from jobfinder.services.resolver.rungs.llm import reinduce_company
from tests.fakes import FakeClaude, fake_resolve_public, handle
from tests.test_rung_llm import induced

PAGE_URL = "https://careers.acme.com/jobs/4021"
PAGE = '<html><body><h1>Careers</h1><script>window.__C = {"api": "/api/v2/jobs"}</script></body></html>'
OLD = {
    "kind": "json_api", "list": {"url": "https://careers.acme.com/api/v1/jobs"}, "items": "$.jobs[*]",
    "fields": {"external_id": "$.id", "title": "$.title", "url": "https://careers.acme.com/jobs/{external_id}"},
    "provenance": {"induced_by": "llm", "validated_with": PAGE_URL},
}
NEW = induced(list={"url": "https://careers.acme.com/api/v2/jobs"}, items="$.data[*]")


def jobs(n: int) -> dict:
    return {"data": [{"id": 5000 + i, "title": f"Engineer {i}"} for i in range(n)]}


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(http_safety, "resolve_public", fake_resolve_public())


def site(web, n_jobs: int) -> httpx.AsyncClient:
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/api/v2/jobs":
            return httpx.Response(200, json=jobs(n_jobs))
        if request.url.path.startswith("/jobs/"):
            return httpx.Response(200, html=PAGE)
        return httpx.Response(404)  # the old endpoint is gone

    handle(web, handler)
    return httpx.AsyncClient()


async def degraded_board(db, baseline=4, **health_over) -> Company:
    c = Company(name="Acme", ats="recipe", slug="acme.com", origin="user", recipe=OLD, recipe_state="degraded",
                recipe_health={"baseline_yield": baseline, "last_yield": 0, "zero_runs": 2,
                               "last_error": "HTTP 404", **health_over})
    db.add(c)
    await db.commit()
    db.add(Job(source="recipe", external_id="acme.com:1", company_id=c.id, company_name="Acme", title="Old job",
               url="https://careers.acme.com/jobs/1", is_active=True, dedupe_hash="old1"))
    await db.commit()
    return c


async def active_titles(db, company_id):
    return sorted((await db.scalars(sa.select(Job.title).where(Job.company_id == company_id, Job.is_active))).all())


async def test_reinduce_happy_path(maker, web):
    fake = FakeClaude(NEW)
    async with maker() as db:
        c = await degraded_board(db)
        out = await reinduce_company(db, AITasks(fake), c.id, client=site(web, 3))
        company = await db.get(Company, c.id)
        titles = await active_titles(db, c.id)
    assert out["ok"] is True and out["fetched"] == 3 and out["state"] == "active"
    assert company.recipe["list"]["url"] == "https://careers.acme.com/api/v2/jobs"
    prov = company.recipe["provenance"]
    assert prov["induced_by"] == "llm" and prov["validated_with"] == PAGE_URL and prov["reinduced_after"] == "HTTP 404"
    assert company.recipe_state == "active" and company.enabled  # its first run with the new recipe was healthy
    assert company.recipe_health["reinduce_count_7d"] == 1
    assert titles == ["Engineer 0", "Engineer 1", "Engineer 2"]  # the old job is gone, the new ones are in
    p = fake.calls[0]["prompt"]
    assert '<data kind="previous_recipe">' in p and "api/v1/jobs" in p
    assert '<data kind="failure">\nHTTP 404\n</data>' in p
    assert 'kind="json_sample" url="https://careers.acme.com/api/v2/jobs"' in p


async def test_reinduce_needs_half_the_baseline_and_disables_on_failure(maker, web):
    fake = FakeClaude(NEW, NEW)
    async with maker() as db:
        c = await degraded_board(db, baseline=10)
        out = await reinduce_company(db, AITasks(fake), c.id, client=site(web, 3))
        company = await db.get(Company, c.id)
        titles = await active_titles(db, c.id)
    assert out["ok"] is False and "expected at least 5" in out["reason"] and len(fake.calls) == 2
    assert company.recipe == OLD and company.recipe_state == "disabled" and not company.enabled
    assert "re-induction failed" in company.disabled_reason
    assert titles == []  # a disabled board's jobs don't stay up with dead links


async def test_reinduce_respects_the_weekly_cap(maker, web):
    fake = FakeClaude()
    async with maker() as db:
        c = await degraded_board(db, reinduce_window_start=datetime.now(UTC).isoformat(),
                                 reinduce_count_7d=health.REINDUCE_CAP)
        out = await reinduce_company(db, AITasks(fake), c.id, client=site(web, 3))
    assert out["skipped"] == "not due" and fake.calls == []


async def test_reinduce_on_rate_limit_leaves_the_board_degraded(maker, web):
    fake = FakeClaude(AIError("rate_limited", "usage limit"))
    async with maker() as db:
        c = await degraded_board(db)
        out = await reinduce_company(db, AITasks(fake), c.id, client=site(web, 3))
        company = await db.get(Company, c.id)
    assert out["skipped"] == "AI unavailable (rate_limited)" and len(fake.calls) == 1
    assert company.recipe_state == "degraded" and not company.recipe_health.get("reinduce_count_7d")


async def test_reinduce_skips_boards_that_are_not_recipes_or_when_disabled(maker, monkeypatch):
    async with maker() as db:
        c = Company(name="Acme", ats="greenhouse", slug="acme")
        db.add(c)
        await db.commit()
        assert await reinduce_company(db, AITasks(FakeClaude()), c.id) == {"skipped": "not a recipe board"}
        assert await reinduce_company(db, AITasks(FakeClaude()), 999_999) == {"skipped": "not a recipe board"}
        d = await degraded_board(db)
        monkeypatch.setattr(get_settings(), "resolver_llm_enabled", False)
        assert await reinduce_company(db, AITasks(FakeClaude()), d.id) == {"skipped": "llm disabled"}


async def test_handler_runs_the_heal_and_honours_backoff(maker, monkeypatch):
    from jobfinder.services.resolver.rungs import llm

    got = []

    async def fake_heal(db, ai, company_id, client=None):
        got.append(company_id)
        return {"ok": True}

    monkeypatch.setattr(llm, "reinduce_company", fake_heal)
    assert handlers.HANDLERS["reinduce_recipe"] is handlers.reinduce_recipe
    async with maker() as db:
        task = Task(kind="reinduce_recipe", payload={"company_id": 3})
        assert await handlers.reinduce_recipe(db, AITasks(FakeClaude()), task) == {"ok": True}
        db.add(AICall(task="score_match", model="sonnet", duration_ms=1, ok=False, error_kind="rate_limited",
                      created_at=datetime.now(UTC)))
        await db.commit()
        out = await handlers.reinduce_recipe(db, AITasks(FakeClaude()), task)
        assert out["skipped"] == "AI backing off" and "requeued" in out and got == [3]
        [later] = (await db.scalars(sa.select(Task).where(Task.kind == "reinduce_recipe"))).all()
        assert later.payload == {"company_id": 3, "attempt": 1} and later.run_after > datetime.now(UTC)
        # the retries are bounded
        last = Task(kind="reinduce_recipe", payload={"company_id": 3, "attempt": handlers.REINDUCE_RETRIES})
        assert "requeued" not in await handlers.reinduce_recipe(db, AITasks(FakeClaude()), last)
        monkeypatch.setattr(get_settings(), "resolver_llm_enabled", False)
        assert await handlers.reinduce_recipe(db, AITasks(FakeClaude()), task) == {"skipped": "llm disabled"}
    assert got == [3]


async def test_shrunk_site_heals_with_what_it_still_has(maker, web):
    """Baseline 10, the site now really has 3 jobs (degraded by the 60% rule, but the old recipe ran cleanly):
    a new recipe returning those 3 is accepted rather than disabling the board."""
    async with maker() as db:
        c = await degraded_board(db, baseline=10, last_yield=3, last_error=None, zero_runs=0)
        out = await reinduce_company(db, AITasks(FakeClaude(NEW)), c.id, client=site(web, 3))
    assert out["ok"] is True and out["fetched"] == 3


async def test_unreachable_site_is_no_verdict(maker, web):
    def down(request):
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(503)

    handle(web, down)
    fake = FakeClaude()
    async with maker() as db:
        c = await degraded_board(db)
        out = await reinduce_company(db, AITasks(fake), c.id, client=httpx.AsyncClient())
        company = await db.get(Company, c.id)
        titles = await active_titles(db, c.id)
    assert out["retry"] is True and out["skipped"].startswith("page unavailable") and fake.calls == []
    assert company.recipe_state == "degraded" and titles == ["Old job"]


async def test_falls_back_to_the_site_root_when_the_job_page_is_gone(maker, web):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/":
            return httpx.Response(200, html=PAGE)
        if request.url.path == "/api/v2/jobs":
            return httpx.Response(200, json=jobs(4))
        return httpx.Response(404)

    handle(web, handler)
    async with maker() as db:
        c = await degraded_board(db)
        out = await reinduce_company(db, AITasks(FakeClaude(NEW)), c.id, client=httpx.AsyncClient())
    assert out["ok"] is True and "/jobs/4021" in seen and "/" in seen


async def test_ingest_queues_a_reinduction_once_the_recipe_degrades(maker, web):
    async with maker() as db:
        c = Company(name="Acme", ats="recipe", slug="acme.com", origin="user", recipe=OLD, recipe_state="active",
                    recipe_health={"baseline_yield": 4, "last_yield": 4})
        db.add(c)
        await db.commit()
        async with site(web, 3) as client:  # OLD's endpoint now 404s
            await ingest_company(db, client, c)
            assert (await db.scalars(sa.select(Task).where(Task.kind == "reinduce_recipe"))).all() == []
            await ingest_company(db, client, c)  # second failure in a row: degraded
            await ingest_company(db, client, c)  # still degraded: the queued task is reused
        company = await db.get(Company, c.id)
        tasks = (await db.scalars(sa.select(Task).where(Task.kind == "reinduce_recipe"))).all()
    assert company.recipe_state == "degraded"
    assert [t.payload for t in tasks] == [{"company_id": c.id}]


async def test_ingest_does_not_queue_when_llm_is_off(maker, monkeypatch, web):
    monkeypatch.setattr(get_settings(), "resolver_llm_enabled", False)
    async with maker() as db:
        c = Company(name="Acme", ats="recipe", slug="acme.com", origin="user", recipe=OLD, recipe_state="active",
                    recipe_health={"baseline_yield": 4, "zero_runs": 1})
        db.add(c)
        await db.commit()
        async with site(web, 3) as client:
            await ingest_company(db, client, c)
        assert (await db.get(Company, c.id)).recipe_state == "degraded"
        assert (await db.scalars(sa.select(Task).where(Task.kind == "reinduce_recipe"))).all() == []
