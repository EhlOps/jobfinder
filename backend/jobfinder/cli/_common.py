"""What every command shares: running its coroutine, its one session, and the exit codes.

A command parses its options (Typer: a usage error exits 2), then `run`s one coroutine that opens one
`transaction()`, calls the services the routers use, and commits at the end. An AppError from a
service prints its message on stderr and exits 1; the coroutine's own return value is the exit code
otherwise (None is 0).
"""
import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from typing import Any

import typer
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.core.context import default_context
from jobfinder.core.db import Sessions
from jobfinder.core.errors import AppError
from jobfinder.tasks import ledger


def sessions() -> Sessions:
    """The process's session factory (default_context().sessions; the tests swap in theirs)."""
    return default_context().sessions


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncSession]:
    """The command's one session: commits when the block ends, rolls back when it raises."""
    async with sessions()() as db:
        yield db
        await db.commit()


def fail(message: str) -> None:
    typer.echo(message, err=True)


def run(work: Coroutine[Any, Any, int | None]) -> None:
    """Run a command's coroutine to completion and exit with its code."""
    raise typer.Exit(asyncio.run(_guarded(work)) or 0)


async def _guarded(work: Coroutine[Any, Any, int | None]) -> int | None:
    try:
        return await work
    except AppError as e:
        fail(e.detail)
        return 1
    finally:
        await _drain_publishes()


async def _drain_publishes() -> None:
    """A committed ledger row is published to valkey by a task the commit scheduled on this loop; let
    it finish before the loop closes (a lost one would wait for the scheduler's relay)."""
    loop = asyncio.get_running_loop()
    while mine := [t for t in ledger._publishing if t.get_loop() is loop and not t.done()]:
        await asyncio.gather(*mine, return_exceptions=True)
