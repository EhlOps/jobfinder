import asyncio
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder import worker
from jobfinder.models import Task
from jobfinder.scheduling import queue


async def test_handler_that_breaks_the_session_ends_failed_not_running(engine, monkeypatch):
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def bad(db, ai, task):
        await db.execute(sa.text("select * from table_that_does_not_exist"))

    monkeypatch.setitem(worker.HANDLERS, "poison", bad)
    async with maker() as db:
        await queue.enqueue(db, "poison")
        t = await queue.claim(db)
    await worker.run_task(t.id, None, maker)
    async with maker() as db:
        t = await db.get(Task, t.id)
        assert t.status == "failed" and "table_that_does_not_exist" in t.error and t.finished_at


async def test_missing_task_is_a_noop(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    await worker.run_task(999999, None, maker)


async def test_unknown_kind_fails_cleanly(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        await queue.enqueue(db, "no_such_kind")
        t = await queue.claim(db)
    await worker.run_task(t.id, None, maker)
    async with maker() as db:
        assert (await db.get(Task, t.id)).status == "failed"


async def test_heartbeat_refreshes_locked_at_while_handler_runs(engine, monkeypatch):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "HEARTBEAT_SECONDS", 0.05)
    old = datetime.now(UTC) - timedelta(hours=1)
    seen = {}

    async def slow(db, ai, task):
        await asyncio.sleep(0.4)
        async with maker() as other:
            seen["locked_at"] = (await other.get(Task, task.id)).locked_at
        return {}

    monkeypatch.setitem(worker.HANDLERS, "slow", slow)
    async with maker() as db:
        await queue.enqueue(db, "slow")
        t = await queue.claim(db)
        t.locked_at = old
        await db.commit()
    await worker.run_task(t.id, None, maker)
    assert seen["locked_at"] > old + timedelta(minutes=30)
    async with maker() as db:
        assert (await db.get(Task, t.id)).status == "done"
