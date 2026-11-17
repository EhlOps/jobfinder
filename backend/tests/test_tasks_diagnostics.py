"""On-demand stack dumps (#136): SIGUSR1 logs a live worker's asyncio stacks, SIGUSR2 its threads."""
import asyncio
import contextlib
import logging
import os
import signal

from taskiq import TaskiqEvents

from jobfinder.tasks import brokers, diagnostics


async def test_sigusr1_logs_the_stack_of_a_hung_task(caplog):
    loop = asyncio.get_running_loop()

    async def stuck_here():
        await asyncio.Event().wait()

    async def hung_handler():
        await stuck_here()

    hung = asyncio.create_task(hung_handler(), name="hung_handler")
    diagnostics.install(loop)
    diagnostics.install(loop)  # idempotent
    try:
        await asyncio.sleep(0)  # the task starts and parks in its wait
        with caplog.at_level(logging.WARNING, logger="jobfinder.tasks"):
            os.kill(os.getpid(), signal.SIGUSR1)
            for _ in range(50):  # the loop runs the handler on its next ticks
                await asyncio.sleep(0.01)
                if any("hung_handler" in r.getMessage() for r in caplog.records):
                    break
        [dump] = [r.getMessage() for r in caplog.records if r.name == "jobfinder.tasks"]  # one record
        assert dump.startswith("stack dump (SIGUSR1): ")
        entry = dump.split("asyncio task hung_handler: ")[1].split("\nasyncio task ")[0]
        # The whole await chain, not just the task's own frame: where the handler is actually stuck.
        assert "in hung_handler" in entry and "in stuck_here" in entry and "in wait" in entry
    finally:
        diagnostics.uninstall(loop)
        hung.cancel()


async def test_the_stack_follows_a_wait_inside_an_async_context_manager():
    """Like a handler waiting for a permit in the Claude gate's slot (an @asynccontextmanager)."""
    @contextlib.asynccontextmanager
    async def slot():
        await asyncio.Event().wait()
        yield

    async def hung_handler():
        async with slot():
            pass

    hung = asyncio.create_task(hung_handler(), name="hung_handler")
    await asyncio.sleep(0)
    try:
        stack = diagnostics.await_stack(hung)
        assert "in hung_handler" in stack and "in __aenter__" in stack and "in slot" in stack
        assert stack.rstrip().endswith("await fut")  # down to Event.wait's future
    finally:
        hung.cancel()


async def test_a_failed_install_does_not_stop_the_worker(monkeypatch, caplog):
    def broken(loop=None):
        raise NotImplementedError("no signal support")

    monkeypatch.setattr(diagnostics, "install", broken)
    await diagnostics.on_worker_startup()
    assert "stack-dump signal handlers not installed" in caplog.text


async def test_sigusr2_writes_every_threads_stack_to_stderr(capfd):
    """For a sync call that blocks the loop, where SIGUSR1's handler never runs."""
    loop = asyncio.get_running_loop()
    diagnostics.install(loop)
    try:
        os.kill(os.getpid(), signal.SIGUSR2)
        await asyncio.sleep(0.05)
    finally:
        diagnostics.uninstall(loop)
    err = capfd.readouterr().err
    assert "most recent call first" in err and "test_sigusr2_writes_every_threads_stack_to_stderr" in err


def test_every_broker_installs_the_dumps_in_its_worker():
    for queue, broker in brokers.BROKERS.items():
        assert diagnostics.on_worker_startup in broker.event_handlers[TaskiqEvents.WORKER_STARTUP], queue
