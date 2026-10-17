"""The render queue's handlers (#48): render_capture and render_list, run by the renderer container's
taskiq worker (`python -m jobfinder.renderer` with TASKS_BACKEND=ledger).

One Chromium serves the whole process: the shared `Renderer` is created on first use and closed on
taskiq's WORKER_SHUTDOWN; each task still gets its own browser context. A refused or broken page raises,
and runtime.execute fails that task without retry; the renderer carries on. Playwright is imported only
when the first browser launches, so the api and worker can import this module without it.
"""
from typing import Any

from pydantic import BaseModel, Field
from taskiq import TaskiqEvents, TaskiqState

from jobfinder.renderer import CAPTURE, LIST, browser
from jobfinder.tasks.brokers import render_broker
from jobfinder.tasks.runtime import ledger_task

_renderer: browser.Renderer | None = None


class CapturePayload(BaseModel):
    url: str | None = None


class ListPayload(BaseModel):
    recipe: dict[str, Any] | None = Field(default=None)


def shared_renderer() -> browser.Renderer:
    global _renderer
    if _renderer is None:
        _renderer = browser.Renderer()  # launches nothing until its first task
    return _renderer


@render_broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def close_renderer(state: TaskiqState | None = None) -> None:
    global _renderer
    if _renderer is not None:
        renderer, _renderer = _renderer, None
        await renderer.close()


@ledger_task(render_broker, CAPTURE, payload=CapturePayload, ai=False)
async def render_capture(db, ai, task, payload: CapturePayload) -> dict:
    return await browser.render_capture(shared_renderer(), str(payload.url or ""))


@ledger_task(render_broker, LIST, payload=ListPayload, ai=False)
async def render_list(db, ai, task, payload: ListPayload) -> dict:
    return await browser.render_list(shared_renderer(), payload.recipe or {})
