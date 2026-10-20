"""tasks.ledger reruns (#48): a request made while the same task runs queues one follow-up behind it,
which claim holds back until the run ends; the paths that turn a running row back into a queued one
leave the work to that follow-up."""
import asyncio
from datetime import timedelta

import pytest
import sqlalchemy as sa

from jobfinder.core.clock import utcnow
from jobfinder.models import Task
from jobfinder.scheduling import queue
from jobfinder.tasks import ledger, runtime
from tests.rollback import count


async def count_rows(db) -> int:
    return await db.scalar(sa.select(sa.func.count()).select_from(Task))


async def _running(db, kind="audit_profile", **kw) -> Task:
    t = await ledger.add(db, kind, dedupe=True, **kw)
    return await ledger.claim(db, t.id)


async def test_rerun_during_a_run_queues_one_follow_up(db):
    first = await _running(db)
    assert (await ledger.add(db, "audit_profile", dedupe=True)).id == first.id  # plain dedupe: the running one
    again = await ledger.add(db, "audit_profile", rerun=True)
    assert again.id != first.id and again.status == "queued"
    assert (await ledger.add(db, "audit_profile", rerun=True)).id == again.id
    assert (await ledger.add(db, "audit_profile", dedupe=True)).id == again.id  # the queued one covers it
    assert await count_rows(db) == 2


async def test_rerun_with_nothing_running_dedupes_like_dedupe(db):
    a = await ledger.add(db, "audit_profile", rerun=True)
    assert (await ledger.add(db, "audit_profile", rerun=True)).id == a.id
    assert (await ledger.add(db, "audit_profile", dedupe=True)).id == a.id


async def test_claim_defers_a_rerun_until_its_twin_finishes(db):
    first = await _running(db)
    rerun = await ledger.add(db, "audit_profile", rerun=True)
    rerun.sent_at = utcnow()
    await db.flush()
    assert await ledger.claim(db, rerun.id) is None
    await db.refresh(rerun)
    assert (rerun.status, rerun.sent_at) == ("queued", None)
    assert rerun.run_after > utcnow() + timedelta(seconds=20)

    await ledger.finish(db, first, {})
    rerun.run_after = utcnow() - timedelta(seconds=1)
    await db.flush()
    assert (await ledger.claim(db, rerun.id)).status == "running"


async def test_a_delivered_rerun_waits_and_its_twin_finishing_publishes_it(maker, published):
    ran = []

    async def handler(db, ai, task, payload):
        ran.append(task.id)
        return {}

    async with maker() as db:
        first = await _running(db, "plan_work")
        rerun = await ledger.add(db, "plan_work", rerun=True)
        await db.commit()
    assert await published.settle() == [("io", "plan_work", first.id), ("io", "plan_work", rerun.id)]
    await runtime.execute("plan_work", handler, rerun.id, wants_ai=False)
    async with maker() as db:
        assert (await db.get(Task, rerun.id)).run_after > utcnow() + timedelta(seconds=20)  # committed
        await ledger.finish(db, await db.get(Task, first.id), {})
        await db.commit()
    assert (await published.settle())[2:] == [("io", "plan_work", rerun.id)]  # woken: due now, sent again
    await runtime.execute("plan_work", handler, rerun.id, wants_ai=False)
    assert ran == [rerun.id]


async def test_a_retry_behind_a_queued_rerun_fails_instead(db):
    first = await _running(db)
    rerun = await ledger.add(db, "audit_profile", rerun=True)
    await ledger.fail(db, first, "rate limited", retry=True)
    assert first.status == "failed"
    await db.refresh(rerun)
    assert rerun.status == "queued"


async def test_reap_leaves_the_work_to_a_queued_rerun(db):
    first = await _running(db)
    rerun = await ledger.add(db, "audit_profile", rerun=True)
    lone = await _running(db, "plan_work")
    first.locked_at = lone.locked_at = utcnow() - timedelta(hours=1)
    await db.flush()
    assert await ledger.reap(db) == 1  # only `lone` goes back to the queue
    for t in (first, rerun, lone):
        await db.refresh(t)
    assert (first.status, rerun.status, lone.status) == ("failed", "queued", "queued")
    assert "rerun" in first.error


async def test_legacy_enqueue_still_returns_a_running_twin(maker):
    async with maker() as db:
        a = await queue.enqueue(db, "k", dedupe=True)
        a = await queue.claim(db)
        assert (await queue.enqueue(db, "k", dedupe=True)).id == a.id


async def test_legacy_claim_skips_a_rerun_while_its_twin_runs(maker):
    async with maker() as db:
        first = await _running(db, "plan_work")
        await ledger.add(db, "plan_work", rerun=True)
        await db.commit()
        assert await queue.claim(db) is None
        await queue.complete(db, first, {})
        assert (await queue.claim(db)).kind == "plan_work"


async def test_legacy_reap_leaves_the_work_to_a_queued_rerun(maker):
    async with maker() as db:
        first = await _running(db, "plan_work")
        await ledger.add(db, "plan_work", rerun=True)
        first.locked_at = utcnow() - timedelta(hours=1)
        await db.commit()
        assert await queue.reap_stuck(db) == 0
        await db.refresh(first)
        assert first.status == "failed"


@pytest.mark.real_commit
async def test_concurrent_reruns_converge_on_one_follow_up(maker):
    async with maker() as db:
        first = await _running(db, "plan_work")
        await db.commit()

    async def add():
        async with maker() as db:
            t = await ledger.add(db, "plan_work", rerun=True)
            await asyncio.sleep(0.05)  # hold the transaction open while the others insert
            await db.commit()
            return t.id

    ids = set(await asyncio.gather(*(add() for _ in range(5))))
    assert len(ids) == 1 and first.id not in ids and await count(maker, Task) == 2
    async with maker() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.status == "queued")) == 1
