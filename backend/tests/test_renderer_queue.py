"""The render worker's share of the ledger: render_* tasks go to the render stream only, and callers wait
a bounded time for them."""
import pytest

from jobfinder.integrations import render as renderer
from jobfinder.integrations.render import RenderError, request_render
from jobfinder.models import Task
from jobfinder.tasks.kinds import QUEUES
from tests.fakes import FakeRenderer


def test_only_the_render_stream_delivers_render_tasks():
    assert {k for k, q in QUEUES.items() if q == "render"} == set(renderer.KINDS)


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
        await request_render("ingest_tick", {}, timeout_s=1, session_factory=maker)


@pytest.mark.real_commit
async def test_request_render_commits_its_task_for_the_render_queue(maker, monkeypatch, published):
    monkeypatch.setattr(renderer, "POLL_S", 0.01)
    with pytest.raises(RenderError, match="did not answer"):
        await request_render("render_list", {"recipe": {}}, timeout_s=0.05, session_factory=maker)
    async with maker() as db:
        t = (await db.execute(Task.__table__.select())).one()
    assert t.queue == "render"
    assert await published.settle() == [("render", "render_list", t.id)]
