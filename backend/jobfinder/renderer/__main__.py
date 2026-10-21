"""The renderer container's entrypoint.

  python -m jobfinder.renderer                    the service: run render_* tasks
  python -m jobfinder.renderer capture <url>      one capture without the database; prints a summary
  python -m jobfinder.renderer capture <url> --json   ...or the whole result as JSON

With TASKS_BACKEND=ledger the service is the render queue's taskiq worker (`python -m jobfinder.tasks
render`; handlers in jobfinder.tasks.render), fed ledger ids from the valkey stream. With
TASKS_BACKEND=legacy it is the claim loop below, polling the tasks table for render_* rows.
"""
import asyncio
import logging
import sys

from jobfinder.integrations.render import CAPTURE, LIST

log = logging.getLogger("jobfinder.renderer")
# RENDERER_CONCURRENCY: browser contexts at once; each is capped in time and bytes (fits the 1.5 GB
# limit). RENDERER_POLL_SECONDS: how often an idle renderer looks for work.


async def handle(renderer, kind: str, payload: dict) -> dict:
    from jobfinder.renderer import browser

    if kind == CAPTURE:
        return await browser.render_capture(renderer, str(payload.get("url") or ""))
    if kind == LIST:
        return await browser.render_list(renderer, payload.get("recipe") or {})
    raise ValueError(f"No renderer handler for task kind {kind!r}")


async def run_task(task_id: int, renderer, session_factory) -> None:
    from jobfinder.models import Task
    from jobfinder.scheduling import queue

    async with session_factory() as db:
        task = await db.get(Task, task_id)
        if task is None:
            return
        try:
            result = await handle(renderer, task.kind, task.payload or {})
            await queue.complete(db, task, result)
        except Exception as e:  # a refused or broken page fails its task, never the renderer
            log.warning("render task %s (%s) failed: %s", task_id, task.kind, e)
            await db.rollback()
            task = await db.get(Task, task_id)
            if task is not None:
                await queue.fail(db, task, f"{type(e).__name__}: {e}", retry=False)


async def serve() -> None:
    from jobfinder.core.config import settings
    from jobfinder.db import SessionLocal
    from jobfinder.renderer.browser import Renderer
    from jobfinder.scheduling import queue

    concurrency = settings.renderer_concurrency
    renderer = Renderer()
    running: set[asyncio.Task] = set()
    log.info("renderer started; claiming %s* tasks, %d at a time", queue.RENDER_PREFIX, concurrency)
    try:
        while True:
            running = {t for t in running if not t.done()}
            claimed = None
            if len(running) < concurrency:
                try:
                    async with SessionLocal() as db:
                        claimed = await queue.claim(db, kind_prefix=queue.RENDER_PREFIX)
                except Exception:
                    log.exception("renderer claim failed")
            if claimed:
                running.add(asyncio.create_task(run_task(claimed.id, renderer, SessionLocal)))
                continue
            await asyncio.sleep(settings.renderer_poll_seconds if len(running) < concurrency else 0.5)
    finally:
        await renderer.close()


async def capture_cli(url: str, as_json: bool) -> int:
    from jobfinder.renderer import browser

    renderer = browser.Renderer()
    try:
        result = await browser.render_capture(renderer, url)
    except Exception as e:
        print(f"refused/failed: {type(e).__name__}: {e}")
        return 2
    finally:
        await renderer.close()
    print(browser.dumps(result) if as_json else browser.summary(result))
    return 0


def main(argv: list[str]) -> int:
    from jobfinder.core import logging as core_logging
    from jobfinder.core.config import settings

    if not argv:
        if settings.tasks_backend == "legacy":
            core_logging.configure(settings.log_level)
            asyncio.run(serve())
            return 0
        from jobfinder.tasks import __main__ as tasks_main

        return tasks_main.main(["render"])  # configures logging itself
    core_logging.configure(settings.log_level)
    if argv[0] == "capture" and len(argv) >= 2:
        return asyncio.run(capture_cli(argv[1], "--json" in argv[2:]))
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
