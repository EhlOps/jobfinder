"""Rung 4.5: the opt-in Playwright render worker (.plans/2026-10-13-adaptive-job-scraping).

A separate container (backend/Dockerfile.renderer) runs the render queue's worker, the only one that gets
`render_*` tasks. The api and the other workers never import Playwright: they add a task with
`request_render` and wait a bounded time for its result.

  render_capture {url}     -> {final_url, json_responses: [{url, method, status, content_type, body_preview,
                               truncated, size, request_body}], html}
  render_list    {recipe}  -> {final_url, items: [{external_id, title, url, ...}]}   (rendered_html recipes)

The browser side lives in renderer/browser.py, the sandbox rules in renderer/guard.py.
"""
import asyncio
import time
from datetime import UTC, datetime

from sqlalchemy import update

from jobfinder.models import Task
from jobfinder.tasks import ledger

CAPTURE = "render_capture"
LIST = "render_list"
KINDS = (CAPTURE, LIST)
POLL_S = 1.0


class RenderError(Exception):
    """The renderer refused, failed, or didn't answer in time. Message is safe to log and show admins."""


class RenderUnavailable(RenderError):
    """No renderer answered in time (not running, or busy): says nothing about the page itself."""


def _sessions():
    from jobfinder import db  # late: tests swap db.SessionLocal for the test database

    return db.SessionLocal


async def request_render(kind: str, payload: dict, *, timeout_s: float, session_factory=None) -> dict:
    """Enqueue a render task (deduped on its payload) and wait at most `timeout_s` for its result."""
    if kind not in KINDS:
        raise ValueError(f"unknown render task {kind!r}")
    factory = session_factory or _sessions()
    async with factory() as db:  # committed here: the polls below read it from other sessions
        task_id = (await ledger.add(db, kind, payload, dedupe=True)).id
        await db.commit()
    deadline = time.monotonic() + timeout_s
    while True:
        async with factory() as db:
            task = await db.get(Task, task_id)
            if task is None:
                raise RenderError("render task disappeared")
            if task.status == "done":
                return task.result or {}
            if task.status == "failed":
                raise RenderError(task.error or "render failed")
            if time.monotonic() >= deadline:
                # Nobody picked it up (renderer not running?): don't leave a backlog behind.
                await db.execute(
                    update(Task).where(Task.id == task_id, Task.status == "queued")
                    .values(status="failed", error="no renderer picked this up in time",
                            finished_at=datetime.now(UTC))
                )
                await db.commit()
                raise RenderUnavailable(f"the renderer did not answer within {timeout_s:.0f}s")
        await asyncio.sleep(POLL_S)
