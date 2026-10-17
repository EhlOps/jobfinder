"""tasks.render (#48): the renderer's handlers on the render stream share one browser, fail a broken
page's task (never the renderer), and `python -m jobfinder.renderer` picks its loop by TASKS_BACKEND."""
import subprocess
import sys

import pytest

from jobfinder.core.config import get_settings
from jobfinder.models import Task
from jobfinder.renderer import __main__ as renderer_main
from jobfinder.renderer import browser
from jobfinder.tasks import ledger, render, runtime


class FakeBrowser:
    instances = 0

    def __init__(self):
        FakeBrowser.instances += 1
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_browser(monkeypatch):
    """browser.Renderer and its two entry points replaced; records (kind, renderer, arg) per call."""
    calls = []
    FakeBrowser.instances = 0
    monkeypatch.setattr(browser, "Renderer", FakeBrowser)
    monkeypatch.setattr(render, "_renderer", None)

    async def capture(renderer, url):
        calls.append(("capture", renderer, url))
        if "blocked" in url:
            raise browser.Blocked("URL points to a non-public address")
        return {"final_url": url, "json_responses": [], "html": "<html></html>"}

    async def listing(renderer, recipe):
        calls.append(("list", renderer, recipe))
        return {"final_url": recipe["url"], "items": [{"external_id": "1", "title": "T"}]}

    monkeypatch.setattr(browser, "render_capture", capture)
    monkeypatch.setattr(browser, "render_list", listing)
    return calls


async def _run(maker, kind, payload):
    async with maker() as db:
        t = await ledger.add(db, kind, payload)
        await db.commit()
    await runtime.execute(kind, runtime.REGISTRY[kind], t.id, payload_model=_model(kind), wants_ai=False)
    async with maker() as db:
        return await db.get(Task, t.id)


def _model(kind):
    return render.CapturePayload if kind == "render_capture" else render.ListPayload


def test_both_kinds_are_registered_on_the_render_broker():
    from jobfinder.tasks.brokers import render_broker

    assert {"render_capture", "render_list"} <= set(render_broker.get_all_tasks())
    assert runtime.REGISTRY["render_capture"] is render.render_capture


async def test_capture_and_list_finish_on_one_shared_renderer(maker, fake_browser):
    cap = await _run(maker, "render_capture", {"url": "https://a.example/"})
    lst = await _run(maker, "render_list", {"recipe": {"url": "https://a.example/jobs"}})
    assert (cap.status, cap.result["final_url"]) == ("done", "https://a.example/")
    assert (lst.status, lst.result["items"][0]["external_id"]) == ("done", "1")
    assert [c[0] for c in fake_browser] == ["capture", "list"]
    assert fake_browser[0][1] is fake_browser[1][1] and FakeBrowser.instances == 1
    assert fake_browser[1][2] == {"url": "https://a.example/jobs"}


async def test_a_refused_page_fails_its_task_without_retry(maker, fake_browser):
    t = await _run(maker, "render_capture", {"url": "http://blocked.example/"})
    assert t.status == "failed" and "Blocked" in t.error and "non-public" in t.error
    assert t.attempts == 1  # no retry scheduled
    ok = await _run(maker, "render_capture", {"url": "https://a.example/"})  # the renderer carries on
    assert ok.status == "done" and FakeBrowser.instances == 1


async def test_missing_payload_fields_pass_empty_values_as_before(maker, fake_browser):
    await _run(maker, "render_capture", {})
    assert fake_browser[0][2] == ""


async def test_worker_shutdown_closes_the_shared_renderer(fake_browser):
    r = render.shared_renderer()
    assert render.shared_renderer() is r
    await render.close_renderer(None)
    assert r.closed and render._renderer is None
    await render.close_renderer(None)  # idempotent


def test_main_runs_the_claim_loop_on_the_legacy_backend(monkeypatch):
    ran = []

    async def serve():
        ran.append("legacy")

    monkeypatch.setattr(get_settings(), "tasks_backend", "legacy")
    monkeypatch.setattr(renderer_main, "serve", serve)
    monkeypatch.setattr("jobfinder.tasks.__main__.main", lambda argv: ran.append(argv) or 0)
    assert renderer_main.main([]) == 0 and ran == ["legacy"]


def test_main_delegates_to_the_render_worker_on_the_ledger_backend(monkeypatch):
    ran = []

    async def serve():
        ran.append("legacy")

    monkeypatch.setattr(get_settings(), "tasks_backend", "ledger")
    monkeypatch.setattr(renderer_main, "serve", serve)
    monkeypatch.setattr("jobfinder.tasks.__main__.main", lambda argv: ran.append(argv) or 0)
    assert renderer_main.main([]) == 0 and ran == [["render"]]


def test_importing_the_render_handlers_does_not_import_playwright():
    code = "import sys, jobfinder.tasks.render; assert 'playwright' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)
