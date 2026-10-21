"""tasks.ledger (#48): add flushes into the caller's transaction, publishes only after it commits,
dedupes, claims atomically, and the relay finds what a lost publish left behind."""
import asyncio
from datetime import timedelta

import pytest
import sqlalchemy as sa

from jobfinder.core.clock import utcnow
from jobfinder.core.config import get_settings
from jobfinder.core.errors import Conflict
from jobfinder.models import Task
from jobfinder.tasks import ledger, runtime
from jobfinder.tasks.kinds import QUEUES, queue_for
from tests.rollback import Boom, count


async def test_add_flushes_and_publishes_only_after_commit(maker, published):
    async with maker() as db:
        t = await ledger.add(db, "cover_letter", {"match_id": 1})
        assert t.id and t.queue == "ai" and t.status == "queued"
        assert await published.settle() == []  # not committed yet
        await db.commit()
    assert await published.settle() == [("ai", "cover_letter", t.id)]


async def test_a_rollback_discards_the_row_and_its_publish(maker, published):
    with pytest.raises(Boom):
        async with maker() as db:
            await ledger.add(db, "plan_work")
            raise Boom
    async with maker() as db:
        await db.commit()  # an unrelated commit later must not publish the discarded id
    assert await published.settle() == []
    assert await count(maker, Task) == 0


@pytest.mark.real_commit
async def test_the_after_commit_publish_marks_the_row_sent(maker, published):
    async with maker() as db:
        t = await ledger.add(db, "plan_work")
        await db.commit()
    await published.settle()
    async with maker() as db:
        assert (await db.get(Task, t.id)).sent_at is not None
        assert await ledger.due_for_relay(db, utcnow() + timedelta(minutes=1)) == []  # no second send


@pytest.mark.real_commit
async def test_a_failed_publish_leaves_its_queue_to_the_relay(maker, published, monkeypatch, caplog):
    from jobfinder.tasks import brokers

    calls = []

    async def ai_down(queue, kind, task_id):
        calls.append(queue)
        if queue == "ai":
            raise TimeoutError

    monkeypatch.setattr(brokers, "publish", ai_down)
    async with maker() as db:
        ai = [await ledger.add(db, "cover_letter", {"match_id": i}) for i in range(3)]
        io = await ledger.add(db, "plan_work")
        await db.commit()
    await published.settle()
    assert calls == ["ai", "io"]  # one try per down queue, not one per row
    assert "the relay will retry: TimeoutError" in caplog.text
    async with maker() as db:
        assert [(await db.get(Task, t.id)).sent_at for t in ai] == [None] * 3
        assert (await db.get(Task, io.id)).sent_at is not None
        assert {r[0] for r in await ledger.due_for_relay(db, utcnow() + timedelta(minutes=1))} == {t.id for t in ai}


async def test_a_claimed_row_is_not_marked_sent_late(db):
    t = await ledger.add(db, "plan_work")
    await ledger.claim(db, t.id)
    await ledger.fail(db, t, "fast", retry=True)  # sent_at NULL: the relay must deliver the retry
    await ledger.mark_sent(db, [t.id], utcnow(), unclaimed_only=True)
    await db.refresh(t)
    assert t.sent_at is None


async def test_a_released_savepoint_publishes_nothing_yet(maker, published):
    async with maker() as db:
        t = await ledger.add(db, "plan_work")
        async with db.begin_nested():
            pass
        assert await published.settle() == []  # the outer transaction hasn't committed
        await db.commit()
    assert await published.settle() == [("io", "plan_work", t.id)] and published.marked == [t.id]


async def test_later_rows_wait_for_the_relay(maker, published):
    async with maker() as db:
        await ledger.add(db, "plan_work", run_after=utcnow() + timedelta(minutes=5))
        await db.commit()
    assert await published.settle() == []


async def test_dedupe_returns_the_active_task(maker, published):
    async with maker() as db:
        a = await ledger.add(db, "match_user", user_id=None, dedupe=True)
        b = await ledger.add(db, "match_user", user_id=None, dedupe=True)
        assert a.id == b.id
        await db.commit()
    assert await published.settle() == [("ai", "match_user", a.id)]  # once


async def test_dedupe_survives_the_generic_plan(maker):
    # asyncpg prepares the insert, and after five runs Postgres may plan it generically, where a bound
    # status in ON CONFLICT ... WHERE can't match the partial index (ingest_tick's fan-out, #50).
    async with maker() as db:
        await db.execute(sa.text("SET plan_cache_mode = force_generic_plan"))
        ids = {(await ledger.add(db, "ingest_board", {"company_id": i}, dedupe=True)).id for i in range(8)}
        assert len(ids) == 8


@pytest.mark.real_commit
async def test_concurrent_dedupe_converges_on_one_row(maker):
    async def add():
        async with maker() as db:
            t = await ledger.add(db, "plan_work", dedupe=True)
            await asyncio.sleep(0.05)  # hold the transaction open while the others insert
            await db.commit()
            return t.id

    ids = await asyncio.gather(*(add() for _ in range(5)))
    assert len(set(ids)) == 1 and await count(maker, Task) == 1


@pytest.mark.real_commit
async def test_a_duplicate_delivery_claims_nothing(maker):
    async with maker() as db:
        t = await ledger.add(db, "plan_work")
        await db.commit()

    async def grab():
        async with maker() as db:
            got = await ledger.claim(db, t.id)
            await db.commit()
            return got

    claimed = [x for x in await asyncio.gather(*(grab() for _ in range(4))) if x]
    assert len(claimed) == 1 and claimed[0].status == "running" and claimed[0].attempts == 1


async def test_claim_skips_rows_not_due(db):
    t = await ledger.add(db, "plan_work", run_after=utcnow() + timedelta(minutes=1))
    assert await ledger.claim(db, t.id) is None


async def test_fail_retries_with_backoff_then_gives_up(db):
    t = await ledger.add(db, "plan_work")
    t = await ledger.claim(db, t.id)
    t.sent_at = utcnow()
    await ledger.fail(db, t, "rate limited", retry=True)
    assert (t.status, t.sent_at) == ("queued", None) and t.run_after > utcnow() + timedelta(seconds=20)
    t.attempts = get_settings().task_max_attempts
    await ledger.fail(db, t, "still", retry=True)
    assert t.status == "failed" and t.finished_at


async def test_requeue_runs_a_failed_task_again_once(db, published):
    t = await ledger.add(db, "ai_check", dedupe=True)
    t = await ledger.claim(db, t.id)
    await ledger.fail(db, t, "bad", retry=False)
    again = await ledger.requeue(db, t.id)
    assert (again.status, again.attempts, again.error) == ("queued", 0, None)
    with pytest.raises(Conflict):
        await ledger.requeue(db, t.id)


async def test_a_row_queued_before_its_kind_moved_publishes_to_the_new_stream(db, published):
    t = Task(kind="daily_emails", queue="ai", payload={}, status="failed")  # daily_emails was on ai before #50
    db.add(t)
    await db.flush()
    await ledger.requeue(db, t.id)
    await db.commit()
    assert (await published.settle()) == [("io", "daily_emails", t.id)]


async def test_requeue_turns_a_twin_racing_the_check_into_a_conflict(db, monkeypatch):
    t = await ledger.add(db, "ai_check", dedupe=True)
    t = await ledger.claim(db, t.id)
    await ledger.fail(db, t, "bad", retry=False)
    twin = await ledger.add(db, "ai_check", dedupe=True)  # queued since: only the unique index stops it now
    real_scalar = db.scalar

    async def miss_the_twin(stmt, *a, **kw):  # as if the twin committed right after the check ran
        monkeypatch.setattr(db, "scalar", real_scalar)  # returns None: no twin found

    monkeypatch.setattr(db, "scalar", miss_the_twin)
    with pytest.raises(Conflict, match="identical ai_check task"):
        await ledger.requeue(db, t.id)
    await db.refresh(t)
    assert t.status == "failed" and (await db.get(Task, twin.id)).status == "queued"  # the session still works


async def test_reap_requeues_stale_running_rows_for_the_relay(db):
    t = await ledger.add(db, "plan_work")
    t = await ledger.claim(db, t.id)
    t.locked_at, t.sent_at = utcnow() - timedelta(hours=1), utcnow()
    await db.flush()
    assert await ledger.reap(db) == 1
    await db.refresh(t)
    assert (t.status, t.sent_at) == ("queued", None)


async def test_due_for_relay(db):
    now = utcnow()
    fresh = await ledger.add(db, "plan_work")  # created_at is the transaction's now(): "fresh" until +15s
    later = await ledger.add(db, "ingest_tick", run_after=now + timedelta(hours=1))
    sent = await ledger.add(db, "score_coveted")
    await ledger.mark_sent(db, [sent.id], now)
    assert await ledger.due_for_relay(db, now) == []
    soon = now + timedelta(seconds=30)
    assert await ledger.due_for_relay(db, soon) == [(fresh.id, "io", "plan_work")]
    much_later = now + timedelta(hours=2)
    assert {r[0] for r in await ledger.due_for_relay(db, much_later)} == {fresh.id, later.id, sent.id}


async def test_prune_deletes_only_old_finished_rows_in_batches(db):
    old = utcnow() - timedelta(days=get_settings().task_prune_days + 1)
    recent = utcnow() - timedelta(days=1)
    rows = {
        name: Task(kind="plan_work", status=status, finished_at=at, created_at=old, run_after=old)
        for name, status, at in [
            ("old_done", "done", old), ("old_failed", "failed", old - timedelta(hours=1)),
            ("recent_done", "done", recent), ("unfinished_done", "done", None),
            # A queued or running row is never pruned, even with a stale finished_at (a retry).
            ("queued", "queued", old), ("running", "running", old),
        ]
    }
    db.add_all(rows.values())
    await db.flush()
    ids = {name: t.id for name, t in rows.items()}
    left = lambda: db.scalars(sa.select(Task.id))
    assert await ledger.prune(db, batch=1) == 1  # the oldest first
    assert ids["old_failed"] not in set(await left())
    assert await ledger.prune(db) == 1
    assert await ledger.prune(db) == 0
    assert set(await left()) == {ids[n] for n in ("recent_done", "unfinished_done", "queued", "running")}


def test_every_handler_kind_has_a_queue():
    import jobfinder.tasks.ai
    import jobfinder.tasks.io  # noqa: F401  (registers their handlers)
    from jobfinder.integrations.render import KINDS

    assert set(runtime.REGISTRY) | set(KINDS) == set(QUEUES)
    with pytest.raises(ValueError):
        queue_for("nope")


async def test_runtime_claims_runs_and_finishes(maker):
    seen = []

    async def handler(db, ai, task, payload):
        seen.append((ai, payload))
        return {"ok": True}

    async with maker() as db:
        t = await ledger.add(db, "plan_work")
        await db.commit()
    await runtime.execute("plan_work", handler, t.id, wants_ai=False)
    await runtime.execute("plan_work", handler, t.id, wants_ai=False)  # redelivery: dropped
    async with maker() as db:
        row = await db.get(Task, t.id)
        assert (row.status, row.result, len(seen)) == ("done", {"ok": True}, 1)


async def test_runtime_records_a_failure(maker):
    async def handler(db, ai, task, payload):
        db.add(Task(kind="x", queue="io", payload={}))  # rolled back with the failure
        await db.flush()
        raise RuntimeError("broken")

    async with maker() as db:
        t = await ledger.add(db, "plan_work")
        await db.commit()
    await runtime.execute("plan_work", handler, t.id, wants_ai=False)
    async with maker() as db:
        row = await db.get(Task, t.id)
        assert row.status == "failed" and "broken" in row.error
        assert await db.scalar(sa.select(sa.func.count()).select_from(Task)) == 1

