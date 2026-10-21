"""The resolver split across queues (#50): resolve_job_link runs the deterministic rungs on io and, when none
reads the site, hands the link to induce_recipe on ai. worker-io never invokes Claude."""
import pytest
import sqlalchemy as sa

from jobfinder.integrations.claude.claude_code import ClaudeCode
from jobfinder.integrations.claude.tasks import AITasks
from jobfinder.models import Company, Task
from jobfinder.tasks import runtime
from jobfinder.tasks.kinds import QUEUES
from tests.fakes import FakeClaude, serve
from tests.test_job_links import GH, GOOD
from tests.test_rung_llm import PAGE, PASTED, induced, site

SEARCH_PAGE = PAGE.replace("/api/jobs?size=50", "/api/search")  # POST only: no deterministic rung reads it
RECIPE = induced(list={"method": "POST", "url": "https://careers.acme.com/api/search", "json_body": {"q": ""}})


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    from jobfinder.integrations import http_safety
    from tests.fakes import fake_resolve_public

    monkeypatch.setattr(http_safety, "resolve_public", fake_resolve_public())


@pytest.fixture
def claude_guard(monkeypatch):
    """Fails the test if anything invokes Claude while `on_io` is set: the real client and AITasks' induction."""
    state = {"on_io": True}
    real_induce = AITasks.induce_recipe

    async def no_cli(self, *a, **kw):
        raise AssertionError("the Claude CLI was invoked")

    async def induce(self, *a, **kw):
        if state["on_io"]:
            raise AssertionError("Claude was asked for a recipe on the io queue")
        return await real_induce(self, *a, **kw)

    monkeypatch.setattr(ClaudeCode, "run", no_cli)
    monkeypatch.setattr(AITasks, "induce_recipe", induce)
    return state


class RaisingClaude(FakeClaude):
    async def run(self, *a, **kw):
        raise AssertionError("FakeClaude called on the io path")


async def _rows(maker, kind) -> list[Task]:
    async with maker() as db:
        return (await db.scalars(sa.select(Task).where(Task.kind == kind).order_by(Task.id))).all()


async def _deliver(kind, task_id, *, ai=None):
    """What the queue's worker does with one delivery."""
    await runtime.execute(kind, runtime.REGISTRY[kind], task_id, payload_model=runtime.PAYLOADS[kind],
                          wants_ai=QUEUES[kind] == "ai", ai=ai)


def test_the_resolver_kinds_are_on_the_right_queues():
    assert (QUEUES["resolve_job_link"], QUEUES["audit_coverage"], QUEUES["induce_recipe"]) == ("io", "io", "ai")


async def test_a_link_no_rung_reads_waits_for_ai_then_resolves(authed, maker, web, published, claude_guard):
    site(web, page=SEARCH_PAGE)
    r = await authed.post("/api/job-links", json={"url": PASTED})
    assert r.status_code == 202, r.text
    link_id = r.json()["id"]
    [resolve] = await _rows(maker, "resolve_job_link")
    assert resolve.queue == "io"

    await _deliver("resolve_job_link", resolve.id, ai=AITasks(RaisingClaude()))
    [resolve] = await _rows(maker, "resolve_job_link")
    assert resolve.status == "done", resolve.error
    assert resolve.result["status"] == "resolving" and resolve.result["queued"] == "induce_recipe"
    [induce] = await _rows(maker, "induce_recipe")
    assert (induce.queue, induce.status, induce.user_id) == ("ai", "queued", resolve.user_id)
    assert induce.payload["link_id"] == link_id
    await published.settle()
    assert ("ai", "induce_recipe", induce.id) in published
    body = (await authed.get(f"/api/job-links/{link_id}")).json()
    assert body["status"] == "resolving"  # the SPA keeps polling

    claude_guard["on_io"] = False
    fake = FakeClaude(RECIPE)
    await _deliver("induce_recipe", induce.id, ai=AITasks(fake))
    [induce] = await _rows(maker, "induce_recipe")
    assert induce.status == "done", induce.error
    assert induce.result["status"] == "resolved" and induce.result["rung"] == "llm" and induce.result["new"] == 3
    assert len(fake.calls) == 1
    body = (await authed.get(f"/api/job-links/{link_id}")).json()
    assert body["status"] == "resolved" and body["job_count"] == 3
    async with maker() as db:
        company = await db.scalar(sa.select(Company).where(Company.ats == "recipe", Company.slug == "acme.com"))
    assert company is not None and company.recipe["provenance"]["induced_by"] == "llm"
    assert [t.status for t in await _rows(maker, "plan_work")] == ["queued"]


async def test_a_deterministic_hit_never_queues_induce_recipe(authed, maker, web, published, claude_guard):
    serve(web, json=GOOD)
    r = await authed.post("/api/job-links", json={"url": GH})
    assert r.status_code == 202, r.text
    [resolve] = await _rows(maker, "resolve_job_link")
    await _deliver("resolve_job_link", resolve.id, ai=AITasks(RaisingClaude()))
    [resolve] = await _rows(maker, "resolve_job_link")
    assert resolve.status == "done" and resolve.result["status"] == "resolved", resolve.error
    assert resolve.result["rung"] == "url_pattern"
    assert await _rows(maker, "induce_recipe") == []
    body = (await authed.get(f"/api/job-links/{r.json()['id']}")).json()
    assert body["status"] == "resolved"


async def test_with_the_llm_off_the_io_step_finishes_the_link(authed, maker, web, monkeypatch, claude_guard):
    from jobfinder.core.config import get_settings

    monkeypatch.setattr(get_settings(), "resolver_llm_enabled", False)
    site(web, page=SEARCH_PAGE)
    r = await authed.post("/api/job-links", json={"url": PASTED})
    [resolve] = await _rows(maker, "resolve_job_link")
    await _deliver("resolve_job_link", resolve.id)
    assert await _rows(maker, "induce_recipe") == []
    body = (await authed.get(f"/api/job-links/{r.json()['id']}")).json()
    assert body["status"] == "unresolved" and "can't read" in body["error"]


async def test_an_unreadable_page_is_not_sent_to_ai(authed, maker, web, claude_guard):
    web.route().respond(404)
    r = await authed.post("/api/job-links", json={"url": PASTED})
    [resolve] = await _rows(maker, "resolve_job_link")
    await _deliver("resolve_job_link", resolve.id)
    assert await _rows(maker, "induce_recipe") == []
    body = (await authed.get(f"/api/job-links/{r.json()['id']}")).json()
    assert body["status"] == "unresolved"


async def test_every_io_kind_is_delivered_without_an_ai_client(monkeypatch):
    """worker-io's deliveries ask the runtime for no Claude client (ledger_task(..., ai=False))."""
    from jobfinder.tasks import brokers

    got = {}

    async def execute(kind, handler, task_id, *, payload_model=None, wants_ai=True, **kw):
        got[kind] = wants_ai

    monkeypatch.setattr(runtime, "execute", execute)
    io_kinds = [k for k, q in QUEUES.items() if q == "io"]
    for kind in io_kinds:
        await brokers.io_broker.find_task(kind).original_func(1)
    assert got == dict.fromkeys(io_kinds, False)


def test_hints_drop_nul_characters_jsonb_cannot_store():
    from jobfinder.services.resolver import _no_nul

    assert _no_nul({"samples": [("u", "a\x00b")], "single_job": {"title": "x\x00"}, "n": 1}) == \
        {"samples": [["u", "ab"]], "single_job": {"title": "x"}, "n": 1}



@pytest.mark.parametrize("kind,payload,patch,out", [
    ("resolve_job_link", {"link_id": 1}, "jobfinder.services.resolver.resolve_link", {"new": 2}),
    ("audit_coverage", {}, "jobfinder.services.coverage.audit_coverage", {"resolved": 1}),
    ("induce_recipe", {"link_id": 1}, "jobfinder.services.resolver.induce_link", {"new": 2}),
])
async def test_new_jobs_are_planned_even_while_a_plan_runs(maker, monkeypatch, kind, payload, patch, out):
    """A plan_work running on older jobs must not absorb the follow-up (rerun, not dedupe)."""
    from jobfinder.tasks import ledger

    async def done(*a, **kw):
        return dict(out)

    async def allowed(db):
        return True

    monkeypatch.setattr(patch, done)
    monkeypatch.setattr("jobfinder.services.resolver.llm_allowed", allowed)
    async with maker() as db:
        running = await ledger.add(db, "plan_work", rerun=True)
        await ledger.claim(db, running.id)
        task = await ledger.add(db, kind, payload)
        await db.commit()
    await _deliver(kind, task.id, ai=AITasks(RaisingClaude()))
    [task] = [t for t in await _rows(maker, kind) if t.id == task.id]
    assert task.status == "done", task.error
    assert sorted(t.status for t in await _rows(maker, "plan_work")) == ["queued", "running"]
