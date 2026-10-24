"""On-demand stack dumps from a live worker (#136): SIGUSR1 logs every asyncio task's stack, SIGUSR2
writes every thread's stack to stderr.

A task hung inside a live worker for over a day and nothing showed where: the pod looked healthy, and a
restart would have thrown the evidence away. Each broker installs these at WORKER_STARTUP (brokers.py),
so they run in taskiq's worker child, not in its main process (which manages the child with SIGTERM,
SIGINT and SIGHUP, and ignores these two: tasks/__main__.py). SIGUSR1 is answered on the event loop, so
it shows what each coroutine awaits, down its await chain; when a synchronous call blocks the loop it
never runs, and SIGUSR2 (faulthandler, which needs no loop) shows the blocking thread instead.
Trigger: `kill -USR1 <worker child pid>`, then read the pod log; docs/k3s.md "Logs & troubleshooting"
finds the pid.
"""
import asyncio
import faulthandler
import logging
import os
import signal
import traceback

from taskiq import TaskiqState

log = logging.getLogger("jobfinder.tasks")


def await_stack(task: asyncio.Task) -> str:
    """The task's stack down its await chain, outermost first (the task's repr names the future it
    waits on). Task.print_stack shows only the task's own coroutine frame, so a handler stuck several
    awaits deep would show only as the taskiq frame that awaits it."""
    frames, obj = [], task.get_coro()
    while (frame := _frame(obj)) is not None:
        frames.append((frame, frame.f_lineno))
        obj = _awaited(obj, frame)
    return "".join(traceback.StackSummary.extract(frames).format()).rstrip()


def _frame(obj):
    return getattr(obj, "cr_frame", None) or getattr(obj, "gi_frame", None) or getattr(obj, "ag_frame", None)


def _awaited(obj, frame):
    awaited = next((a for a in (getattr(obj, n, None) for n in ("cr_await", "gi_yieldfrom", "ag_await"))
                    if a is not None), None)
    if awaited is not None and _frame(awaited) is None:
        # An async generator's asend() hides the generator; an @asynccontextmanager (the Claude gate's
        # slot) keeps it on self.gen, so a wait inside one is followed into its body.
        gen = getattr(frame.f_locals.get("self"), "gen", None)
        if getattr(gen, "ag_frame", None) is not None:
            return gen
    return awaited


def dump_tasks(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Log every asyncio task on the loop (name, state, stack) as one WARNING, so the dump stays whole
    in the pod log."""
    tasks = sorted(asyncio.all_tasks(loop), key=lambda t: t.get_name())
    log.warning("stack dump (SIGUSR1): %d asyncio tasks in pid %d\n%s", len(tasks), os.getpid(),
                "\n".join(f"asyncio task {t.get_name()}: {t!r}\n{await_stack(t)}" for t in tasks))


def install(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Answer SIGUSR1 with dump_tasks on `loop` (the running loop by default) and SIGUSR2 with a thread
    dump to stderr. Installing again just replaces both handlers."""
    loop = loop or asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGUSR1, dump_tasks, loop)
    faulthandler.register(signal.SIGUSR2, all_threads=True)


def uninstall(loop: asyncio.AbstractEventLoop) -> None:
    loop.remove_signal_handler(signal.SIGUSR1)
    faulthandler.unregister(signal.SIGUSR2)


async def on_worker_startup(state: TaskiqState | None = None) -> None:
    try:
        install()
    except Exception:  # a debugging aid must never keep the worker from starting
        log.exception("stack-dump signal handlers not installed")
