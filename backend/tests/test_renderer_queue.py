"""The render worker's share of the task queue: render_* tasks go to the renderer only, and callers wait a
bounded time for them."""
import pytest

from jobfinder import renderer
from jobfinder.models import Task
from jobfinder.renderer import RenderError, request_render
from jobfinder.scheduling import queue
from tests.fakes import FakeRenderer


async def test_main_worker_never_claims_render_tasks(maker):
    async with maker() as db:
        await queue.enqueue(db, "render_capture", {"url": "https://a.example/"})
        await queue.enqueue(db, "render_list", {"recipe": {}})
        plain = await queue.enqueue(db, "ingest_jobs")
        got = await queue.claim(db, exclude_prefix=queue.RENDER_PREFIX)
        assert got.id == plain.id
        assert await queue.claim(db, exclude_prefix=queue.RENDER_PREFIX) is None


async def test_renderer_claims_only_render_tasks(maker):
    async with maker() as db:
        await queue.enqueue(db, "ingest_jobs")
        await queue.enqueue(db, "renderXcapture")  # '_' must not act as a LIKE wildcard
        r = await queue.enqueue(db, "render_capture", {"url": "https://a.example/"})
        got = await queue.claim(db, kind_prefix=queue.RENDER_PREFIX)
        assert (got.id, got.kind) == (r.id, "render_capture")
        assert await queue.claim(db, kind_prefix=queue.RENDER_PREFIX) is None
        assert (await queue.claim(db)).kind in ("ingest_jobs", "renderXcapture")  # unfiltered: unchanged


async def test_worker_loop_passes_the_exclude_filter():
    import inspect

    from jobfinder import worker

    assert "exclude_prefix=queue.RENDER_PREFIX" in inspect.getsource(worker.main)


@pytest.mark.real_commit
async def test_request_render_returns_the_result(maker, monkeypatch):
    monkeypatch.setattr(renderer, "POLL_S", 0.01)
    async with FakeRenderer(maker, {"render_capture": {"final_url": "https://a.example/"}}):
        got = await request_render("render_capture", {"url": "https://a.example/"}, timeout_s=5,
                                   session_factory=maker)
    assert got == {"final_url": "https://a.example/"}


@pytest.mark.real_commit
async def test_request_render_reports_failure(maker, monkeypatch):
    monkeypatch.setattr(renderer, "POLL_S", 0.01)
    async with FakeRenderer(maker, {}, fail="Blocked: URL points to a non-public address"):
        with pytest.raises(RenderError, match="non-public"):
            await request_render("render_capture", {"url": "http://169.254.169.254/"}, timeout_s=5,
                                 session_factory=maker)


async def test_request_render_times_out_and_withdraws_the_task(maker, monkeypatch):
    monkeypatch.setattr(renderer, "POLL_S", 0.01)
    with pytest.raises(RenderError, match="did not answer"):
        await request_render("render_capture", {"url": "https://a.example/"}, timeout_s=0.05,
                             session_factory=maker)
    async with maker() as db:
        t = (await db.execute(Task.__table__.select())).one()
        assert t.status == "failed" and "no renderer" in t.error  # no backlog left for a renderer that's off


async def test_request_render_rejects_unknown_kinds(maker):
    with pytest.raises(ValueError):
        await request_render("ingest_jobs", {}, timeout_s=1, session_factory=maker)
