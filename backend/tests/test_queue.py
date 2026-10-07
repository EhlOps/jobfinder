import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.scheduling import queue


async def test_enqueue_claim_complete(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        t = await queue.enqueue(db, "k", {"a": 1})
        got = await queue.claim(db)
        assert got.id == t.id and got.status == "running" and got.attempts == 1
        assert await queue.claim(db) is None
        await queue.complete(db, got, {"ok": True})
        assert (got.status, got.result) == ("done", {"ok": True})


async def test_concurrent_claims_never_return_same_task(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        for _ in range(5):
            await queue.enqueue(db, "k")

    async def grab():
        async with maker() as db:
            return await queue.claim(db)

    claimed = [t for t in await asyncio.gather(*(grab() for _ in range(8))) if t]
    assert len(claimed) == 5 and len({t.id for t in claimed}) == 5


async def test_retry_backoff_then_permanent_failure(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        await queue.enqueue(db, "k")
        t = await queue.claim(db)
        await queue.fail(db, t, "rate limited", retry=True)
        assert t.status == "queued" and t.run_after > datetime.now(UTC) + timedelta(seconds=20)
        assert await queue.claim(db) is None  # not due yet

        t.attempts = queue.MAX_ATTEMPTS
        await queue.fail(db, t, "still failing", retry=True)
        assert t.status == "failed" and t.finished_at

        await queue.enqueue(db, "k2")
        t2 = await queue.claim(db)
        await queue.fail(db, t2, "bad", retry=False)
        assert t2.status == "failed"


async def test_dedupe_and_reap(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        a = await queue.enqueue(db, "k", user_id=None, dedupe=True)
        b = await queue.enqueue(db, "k", user_id=None, dedupe=True)
        assert a.id == b.id

        t = await queue.claim(db)
        t.locked_at = datetime.now(UTC) - timedelta(hours=1)
        await db.commit()
        assert await queue.reap_stuck(db) == 1
        assert (await queue.claim(db)).id == a.id


async def test_concurrent_dedupe_converges_on_one_task(engine):
    import sqlalchemy as sa

    from jobfinder.models import Task

    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def go():
        async with maker() as db:
            return (await queue.enqueue(db, "match_user", {"a": 1}, user_id=None, dedupe=True)).id

    ids = await asyncio.gather(*(go() for _ in range(10)))
    assert len(set(ids)) == 1
    async with maker() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(Task)) == 1


async def test_dedupe_distinguishes_payloads_and_frees_key_when_done(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        a = await queue.enqueue(db, "k", {"x": 1}, dedupe=True)
        b = await queue.enqueue(db, "k", {"x": 2}, dedupe=True)
        assert a.id != b.id
        t = await queue.claim(db)
        assert (await queue.enqueue(db, "k", {"x": 1}, dedupe=True)).id == a.id  # still running
        await queue.complete(db, t, {})
        again = await queue.enqueue(db, "k", {"x": t.payload["x"]}, dedupe=True)
        assert again.id not in (a.id, b.id) and again.status == "queued"


async def test_non_dedupe_enqueue_still_makes_duplicates(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        a = await queue.enqueue(db, "ai_check")
        b = await queue.enqueue(db, "ai_check")
        assert a.id != b.id


async def test_heartbeat_keeps_running_task_from_being_reaped(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        await queue.enqueue(db, "k")
        t = await queue.claim(db)
        t.locked_at = datetime.now(UTC) - timedelta(hours=1)
        await db.commit()
        await queue.heartbeat(db, t.id)
        assert await queue.reap_stuck(db) == 0
        await db.refresh(t)
        assert t.status == "running"


async def test_reap_fails_tasks_out_of_attempts_and_requeues_others(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        await queue.enqueue(db, "poison")
        await queue.enqueue(db, "fresh")
        poison, fresh = await queue.claim(db), await queue.claim(db)
        poison.attempts = queue.MAX_ATTEMPTS
        for t in (poison, fresh):
            t.locked_at = datetime.now(UTC) - timedelta(hours=1)
        await db.commit()
        assert await queue.reap_stuck(db) == 1
        await db.refresh(poison)
        await db.refresh(fresh)
        assert poison.status == "failed" and poison.finished_at and "stopped responding" in poison.error
        assert fresh.status == "queued"
