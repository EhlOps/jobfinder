"""jobfinder.scheduler (#48): the cron table follows its settings and writes deduped ledger rows, the
relay re-sends what the after-commit publish missed (and survives valkey being down), and maintenance
reaps."""
import asyncio
import logging
import os
import signal
from datetime import timedelta

import pytest
import sqlalchemy as sa

from jobfinder import scheduler as pkg
from jobfinder.core.clock import next_on_grid, utcnow
from jobfinder.core.config import get_settings
from jobfinder.models import Task
from jobfinder.scheduler import __main__ as entry
from jobfinder.scheduler import cron, maintenance, relay
from jobfinder.tasks import brokers, ledger


@pytest.fixture(autouse=True)
def alive_file(tmp_path, monkeypatch):
    path = tmp_path / "scheduler-alive"
    monkeypatch.setattr(pkg, "ALIVE_FILE", path)
    return path


def _jobs(scheduler) -> dict:
    return {j.id: (j.args, str(j.trigger)) for j in scheduler.get_jobs()}


@pytest.mark.parametrize("adaptive", [True, False])
@pytest.mark.parametrize("discovery,coverage", [(False, False), (True, False), (True, True)])
def test_the_cron_table(monkeypatch, adaptive, discovery, coverage):
    s = get_settings()
    for name, value in [("adaptive_schedule_enabled", adaptive), ("discovery_enabled", discovery),
                        ("coverage_enabled", coverage), ("ingest_interval_hours", 3), ("ingest_tick_minutes", 15),
                        ("planner_interval_minutes", 20)]:
        monkeypatch.setattr(s, name, value)
    before = utcnow()
    scheduler = cron.make_scheduler()
    jobs = _jobs(scheduler)
    assert set(jobs) == {"ingest_tick", "plan_work", "daily_emails"} | ({"discover_boards"} if discovery else set()) \
        | ({"score_coveted"} if coverage else set())
    assert all(args == (kind,) for kind, (args, _) in jobs.items())
    assert jobs["ingest_tick"][1] == ("interval[0:15:00]" if adaptive else "interval[3:00:00]")
    assert jobs["plan_work"][1] == "interval[0:20:00]"
    now = utcnow()
    for job in scheduler.get_jobs():  # the interval jobs first run a few minutes after start
        if job.id == "ingest_tick":
            assert abs(job.next_run_time - (now + timedelta(minutes=2))) < timedelta(seconds=5)
        if job.id == "plan_work":  # the first grid point after start + 5 minutes (either side of a boundary)
            assert job.next_run_time in {next_on_grid(t + timedelta(minutes=5), timedelta(minutes=20)) for t in (before, now)}
    plan = scheduler.get_job("plan_work").trigger  # then every 20 minutes on the grid, from wherever it ran
    later = next_on_grid(now, timedelta(minutes=20)) + timedelta(seconds=1)
    assert plan.get_next_fire_time(None, later) == next_on_grid(later, timedelta(minutes=20))
    assert scheduler._job_defaults["coalesce"] is True and scheduler._job_defaults["max_instances"] == 1


@pytest.mark.real_commit
async def test_a_cron_run_adds_one_deduped_row_and_commits(maker, published):
    await cron.run("ingest_tick")
    await cron.run("ingest_tick")  # the previous run is still queued: not duplicated
    await cron.run("daily_emails")
    async with maker() as db:  # a fresh connection: only committed rows are visible
        rows = (await db.execute(sa.select(Task.kind, Task.dedupe_key))).all()
    assert sorted(k for k, _ in rows) == ["daily_emails", "ingest_tick"] and all(key for _, key in rows)
    assert sorted(k for _, k, _ in await published.settle()) == ["daily_emails", "ingest_tick"]


async def test_a_failing_cron_run_is_logged_not_raised(monkeypatch, caplog):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ledger, "add", boom)
    await cron.run("plan_work")
    assert "scheduling plan_work failed" in caplog.text


async def _relay_rows(maker, published):
    """One row of each sort the relay must tell apart, committed; the after-commit publishes cleared."""
    now = utcnow()
    async with maker() as db:
        due = await ledger.add(db, "plan_work")
        later = await ledger.add(db, "ingest_tick", run_after=now + timedelta(hours=1))
        sent = await ledger.add(db, "score_coveted")
        await ledger.mark_sent(db, [sent.id], now)
        await db.commit()
    (await published.settle()).clear()
    return due, later, sent


async def test_the_relay_publishes_due_unsent_rows_once(maker, published, alive_file):
    due, later, sent = await _relay_rows(maker, published)
    assert await relay.tick(utcnow()) == 0  # everything is within the after-commit publish's grace
    now = utcnow() + timedelta(seconds=30)  # created_at is fixed per test, so step past the grace
    assert await relay.tick(now) == 1
    assert published == [("io", "plan_work", due.id)]
    async with maker() as db:
        assert (await db.get(Task, due.id)).sent_at == now
        assert (await db.get(Task, later.id)).sent_at is None
        assert (await db.get(Task, sent.id)).sent_at < now  # relayed a moment ago: left alone
    assert await relay.tick(now + timedelta(seconds=15)) == 0  # sent: not again until RELAY_RESEND
    assert alive_file.exists()


async def test_the_relay_survives_valkey_being_down(maker, published, monkeypatch, caplog, alive_file):
    async with maker() as db:
        rows = [await ledger.add(db, "plan_work", {"n": i}) for i in range(3)]
        await db.commit()
    (await published.settle()).clear()

    valkey = {"up": False}

    async def flaky(queue, kind, task_id):
        if not valkey["up"]:
            raise ConnectionError("valkey unreachable")
        published.append((queue, kind, task_id))

    monkeypatch.setattr(brokers, "publish", flaky)
    now = utcnow() + timedelta(seconds=30)
    with caplog.at_level(logging.WARNING, logger="jobfinder.scheduler"):
        assert await relay.tick(now) == 0
    assert sum("relay: publish failed" in r.message for r in caplog.records) == 1  # one per tick, not per row
    async with maker() as db:
        assert [(await db.get(Task, t.id)).sent_at for t in rows] == [None, None, None]
    assert alive_file.exists()  # still alive: the process is fine, valkey isn't
    valkey["up"] = True
    assert await relay.tick(now + timedelta(seconds=15)) == 3  # valkey back: the next tick sends them all


async def test_one_queue_down_does_not_hold_up_the_others(maker, published, monkeypatch, caplog):
    async with maker() as db:
        ai = await ledger.add(db, "cover_letter", {"match_id": 1})
        io = await ledger.add(db, "plan_work")
        moved = await ledger.add(db, "match_user")
        moved.queue = "io"  # a row queued before its kind moved keeps the old column
        await db.commit()
    (await published.settle()).clear()

    async def ai_down(queue, kind, task_id):
        if queue == "ai":
            raise ConnectionError("ai stream unavailable")
        published.append((queue, kind, task_id))

    monkeypatch.setattr(brokers, "publish", ai_down)
    assert await relay.tick(utcnow() + timedelta(seconds=30)) == 1
    assert published == [("io", "plan_work", io.id)]  # match_user goes to its kind's stream, not 'io'
    assert sum("relay: publish failed" in r.message for r in caplog.records) == 1
    async with maker() as db:
        assert (await db.get(Task, ai.id)).sent_at is None and (await db.get(Task, moved.id)).sent_at is None


async def test_a_failing_relay_tick_is_logged_not_raised(monkeypatch, caplog, alive_file):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ledger, "due_for_relay", boom)
    assert await relay.tick() == 0
    assert "relay tick failed" in caplog.text and alive_file.exists()


async def test_maintenance_reaps_a_stale_running_row(maker):
    async with maker() as db:
        t = await ledger.add(db, "plan_work")
        t = await ledger.claim(db, t.id)
        t.locked_at, t.sent_at = utcnow() - timedelta(hours=1), utcnow()
        await db.commit()
    await maintenance.tick()
    async with maker() as db:
        row = await db.get(Task, t.id)
        assert (row.status, row.sent_at) == ("queued", None)


async def test_maintenance_prunes_old_finished_rows(maker, caplog):
    caplog.set_level(logging.INFO, "jobfinder.scheduler")
    old = utcnow() - timedelta(days=get_settings().task_prune_days + 1)
    async with maker() as db:
        done = Task(kind="plan_work", status="done", finished_at=old)
        queued = Task(kind="plan_work", status="queued", finished_at=old)
        db.add_all([done, queued])
        await db.commit()
    await maintenance.tick()
    async with maker() as db:
        assert await db.get(Task, done.id) is None and await db.get(Task, queued.id) is not None
    assert "pruned 1 finished task(s)" in caplog.text


async def test_a_failing_maintenance_tick_is_logged_not_raised(monkeypatch, caplog):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ledger, "reap", boom)
    await maintenance.tick()
    assert "maintenance tick failed" in caplog.text


def test_the_process_starts_and_returns_on_stop(monkeypatch):
    ran = []

    async def serve():
        ran.append(True)

    monkeypatch.setattr(entry, "serve", serve)
    assert entry.main() == 0 and ran == [True]


@pytest.mark.parametrize("enabled", [True, False])
def test_relay_and_maintenance_always_run_cron_only_when_enabled(monkeypatch, enabled):
    monkeypatch.setattr(get_settings(), "scheduler_enabled", enabled)
    ids = set(_jobs(entry.build()))
    assert {"relay", "maintenance"} <= ids
    assert ("plan_work" in ids) == enabled


async def test_sigterm_lets_running_ticks_finish_then_stops(monkeypatch):
    started, finished = [], []

    async def tick():
        started.append(1)
        await asyncio.sleep(0.2)
        finished.append(1)

    monkeypatch.setattr(get_settings(), "scheduler_enabled", False)
    monkeypatch.setattr(relay, "tick", tick)
    monkeypatch.setattr(maintenance, "tick", tick)
    serving = asyncio.create_task(entry.serve())
    for _ in range(100):  # both run once at start
        if len(started) >= 2:
            break
        await asyncio.sleep(0.01)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(serving, 5)
    assert len(started) >= 2 and len(finished) == len(started)
