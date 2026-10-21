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


@pytest.mark.real_commit
async def test_every_waiter_on_an_unanswered_render_sees_it_unavailable(maker, monkeypatch):
    """Waiters deduped onto one task: the first one's timeout expires it, and the others must not take
    that for a page failure (a RenderError counts against the board)."""
    import asyncio

    from jobfinder.integrations.render import RenderUnavailable

    monkeypatch.setattr(renderer, "POLL_S", 0.01)
    results = await asyncio.gather(
        request_render("render_capture", {"url": "https://a.example/"}, timeout_s=0.05, session_factory=maker),
        request_render("render_capture", {"url": "https://a.example/"}, timeout_s=5, session_factory=maker),
        return_exceptions=True,
    )
    assert [type(r) for r in results] == [RenderUnavailable, RenderUnavailable]
    async with maker() as db:
        assert len((await db.execute(Task.__table__.select())).all()) == 1  # both waited on the one task


@pytest.mark.real_commit
async def test_a_render_claimed_at_the_deadline_is_waited_for(maker, monkeypatch):
    """The renderer took the task just as the waiter's time ran out: it gets one more timeout to finish."""
    import asyncio

    from jobfinder.tasks import ledger

    monkeypatch.setattr(renderer, "POLL_S", 0.01)
    waiter = asyncio.create_task(request_render("render_capture", {"url": "https://a.example/"}, timeout_s=0.3,
                                                session_factory=maker))
    await asyncio.sleep(0.15)
    async with maker() as db:
        [t] = (await db.execute(Task.__table__.select())).all()
        task = await ledger.claim(db, t.id)
        await db.commit()
    await asyncio.sleep(0.3)  # past the first deadline: still running
    async with maker() as db:
        await ledger.finish(db, await db.get(Task, task.id), {"final_url": "https://a.example/"})
        await db.commit()
    assert await waiter == {"final_url": "https://a.example/"}


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
