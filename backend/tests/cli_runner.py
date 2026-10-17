"""Running `jobfinder ...` in a test: Typer's CliRunner on a thread.

A command calls asyncio.run, which can't nest in the test's loop, so it runs on a thread with a loop of
its own. Its sessions open connections on that loop, which only works with the real_commit engine
(NullPool); mark the test @pytest.mark.real_commit when the command touches the database.
"""
import asyncio

from typer.testing import CliRunner

from jobfinder.cli import app

runner = CliRunner()


async def invoke(*args: str):
    return await asyncio.to_thread(runner.invoke, app, list(args))


def invoke_sync(*args: str):
    return runner.invoke(app, list(args))
