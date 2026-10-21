"""The renderer container's entrypoint.

  python -m jobfinder.renderer                    the service: the render queue's taskiq worker
  python -m jobfinder.renderer capture <url>      one capture without the database; prints a summary
  python -m jobfinder.renderer capture <url> --json   ...or the whole result as JSON

The service is `python -m jobfinder.tasks render` (handlers in jobfinder.tasks.render), fed ledger ids
from the valkey stream.
"""
import asyncio
import sys


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
        from jobfinder.tasks import __main__ as tasks_main

        return tasks_main.main(["render"])  # configures logging itself
    core_logging.configure(settings.log_level)
    if argv[0] == "capture" and len(argv) >= 2:
        return asyncio.run(capture_cli(argv[1], "--json" in argv[2:]))
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
