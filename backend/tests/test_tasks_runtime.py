"""tasks.runtime (#48): what a delivery does with every outcome, and that the queues are separate
consumers (a long io task never holds up an ai one, nor bulk ai work a priority one)."""
import asyncio
from datetime import timedelta

import pytest
import sqlalchemy as sa

from jobfinder.core.clock import utcnow
from jobfinder.core.config import get_settings
from jobfinder.integrations.claude.claude_code import AIError
from jobfinder.models import Task
from jobfinder.tasks import ai as ai_tasks
from jobfinder.tasks import brokers, ledger, runtime
from jobfinder.tasks import io as io_tasks
from jobfinder.tasks import render as render_tasks
from jobfinder.tasks.__main__ import WORKER_MODULES, worker_argv
from jobfinder.tasks.kinds import QUEUES
from tests.rollback import count


async def _queued(maker, kind, payload=None) -> int:
    async with maker() as db:
        t = await ledger.add(db, kind, payload)
        await db.commit()
        return t.id


async def _row(maker, task_id) -> Task:
    async with maker() as db:
        return await db.get(Task, task_id)


def test_each_queue_broker_carries_exactly_its_kinds():
    """Each worker consumes its own queue's broker (worker-priority priority_broker, worker-ai ai_broker,
    worker-io io_broker, ...): no kind is on two."""
    assert ai_tasks and io_tasks and render_tasks  # imported: registered
    for queue in brokers.BROKERS:
        registered = set(brokers.BROKERS[queue].local_task_registry)
        assert registered == {k for k, q in QUEUES.items() if q == queue}
        # and each queue's worker process consumes only its own stream, with its own concurrency
        argv = worker_argv(queue)
        assert argv[:1 + len(WORKER_MODULES[queue])] == [f"jobfinder.tasks.brokers:{queue}_broker",
                                                          *WORKER_MODULES[queue]]
    assert len({b.queue_name for b in brokers.BROKERS.values()}) == len(brokers.BROKERS)


def test_every_kind_is_registered_on_exactly_its_queues_broker():
    """@ledger_task(kind) takes the broker from kinds.QUEUES, so no handler can sit on another stream."""
    assert ai_tasks and io_tasks and render_tasks
    assert set(runtime.REGISTRY) == set(QUEUES)  # every kind has a handler
    for kind in runtime.REGISTRY:
        on = [queue for queue, broker in brokers.BROKERS.items() if kind in broker.get_all_tasks()]
        assert on == [QUEUES[kind]], kind


def test_the_priority_lane_carries_the_work_someone_waits_on():
    """#124: profile, document, cover-letter and resume kinds skip the bulk ai and io streams; the
    priority worker asks the Claude gate in the priority lane, every other worker in bulk."""
    from jobfinder.tasks.__main__ import PRIORITY_QUEUES

    assert {k for k, q in QUEUES.items() if q == "priority"} == {
        "extract_profile", "followup_questions", "audit_profile", "cover_letter", "resume_tailor", "ai_check",
        "parse_document", "fetch_link"}
    assert {k for k, q in QUEUES.items() if q == "ai"} == {
        "match_user", "collect_questions", "induce_recipe", "reinduce_recipe"}
    assert PRIORITY_QUEUES == {"priority"} and "priority" in brokers.BROKERS
    assert brokers.priority_broker.queue_name == "jobfinder:priority"
    argv = worker_argv("priority")
    assert argv[argv.index("--max-async-tasks") + 1] == str(brokers.settings.worker_priority_concurrency)


def test_each_worker_imports_the_handlers_of_every_kind_on_its_queue():
    """A worker that didn't import a kind's module would leave its messages unhandled on the stream."""
    import json
    import subprocess
    import sys

    assert set(WORKER_MODULES) == set(brokers.BROKERS)
    for queue, modules in WORKER_MODULES.items():
        # A fresh interpreter, as the worker starts: this test process has imported every module already.
        code = (f"import importlib, json\nfor m in {modules!r}: importlib.import_module(m)\n"
                f"from jobfinder.tasks.brokers import BROKERS\n"
                f"print(json.dumps(sorted(BROKERS[{queue!r}].get_all_tasks())))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
        assert set(json.loads(out.splitlines()[-1])) == {k for k, q in QUEUES.items() if q == queue}, queue


def test_each_broker_reads_no_more_than_its_worker_runs_at_once():
    """No read-ahead beyond the concurrency (#52): what a pod has read but not started is stranded on its
    consumer when KEDA scales it in, and is invisible to the lag KEDA scales on."""
    for queue, setting in brokers.CONCURRENCY.items():
        concurrency = getattr(brokers.settings, setting)
        assert brokers.BROKERS[queue].count == max(1, concurrency)
        assert worker_argv(queue)[worker_argv(queue).index("--max-async-tasks") + 1] == str(concurrency)


async def test_the_registered_taskiq_task_runs_the_handler(maker, monkeypatch):
    seen = []

    async def plan(db):
        seen.append(1)
        return {"planned": 0}

    from jobfinder.services import planner

    monkeypatch.setattr(planner, "plan", plan)
    tid = await _queued(maker, "plan_work")
    await brokers.io_broker.find_task("plan_work").original_func(tid)  # what the taskiq receiver calls
    row = await _row(maker, tid)
    assert (row.status, row.result, row.attempts, seen) == ("done", {"planned": 0}, 1, [1])


async def test_io_kinds_get_no_ai_client(maker):
    got = []

    async def handler(db, ai, task, payload):
        got.append(ai)
        return {}

    tid = await _queued(maker, "plan_work")
    await runtime.execute("plan_work", handler, tid, wants_ai=False, ai=object())
    assert got == [None]


async def test_a_retryable_ai_error_queues_it_again_with_backoff(maker):
    async def handler(db, ai, task, payload):
        db.add(Task(kind="x", queue="io", payload={}))  # undone with the failure
        raise AIError("rate_limited", "slow down")

    tid = await _queued(maker, "ai_check")
    await runtime.execute("ai_check", handler, tid, ai=object())
    row = await _row(maker, tid)
    assert (row.status, row.sent_at, row.attempts) == ("queued", None, 1) and "slow down" in row.error
    assert row.run_after > utcnow() + timedelta(seconds=10)
    assert await count(maker, Task) == 1


async def test_a_permanent_ai_error_fails_it(maker):
    async def handler(db, ai, task, payload):
        raise AIError("auth", "logged out")

    tid = await _queued(maker, "ai_check")
    await runtime.execute("ai_check", handler, tid, ai=object())
    assert (await _row(maker, tid)).status == "failed"


async def test_a_payload_that_does_not_validate_fails_without_running(maker):
    ran = []

    async def handler(db, ai, task, payload):
        ran.append(payload)
        return {}

    tid = await _queued(maker, "cover_letter", {"tone": "warm"})  # no match_id
    await runtime.execute("cover_letter", handler, tid, payload_model=ai_tasks.CoverLetterArgs, ai=object())
    row = await _row(maker, tid)
    assert row.status == "failed" and "match_id" in row.error and ran == []


async def test_a_row_of_another_kind_is_not_run(maker):
    async def handler(db, ai, task, payload):
        raise AssertionError("must not run")

    tid = await _queued(maker, "plan_work")
    await runtime.execute("ingest_tick", handler, tid, wants_ai=False)
    row = await _row(maker, tid)
    assert row.status == "failed" and "delivered as 'ingest_tick'" in row.error


async def test_a_delivery_for_a_running_row_is_dropped(maker):
    """A redelivery of a slow task, or of a dead worker's before the reaper requeued it: the claim
    fails and nothing runs. The reaper is what brings a dead worker's row back."""
    ran = []

    async def handler(db, ai, task, payload):
        ran.append(1)
        return {}

    tid = await _queued(maker, "plan_work")
    async with maker() as db:
        t = await ledger.claim(db, tid)
        t.locked_at = utcnow() - timedelta(hours=1)  # its worker died long ago
        await db.commit()
    await runtime.execute("plan_work", handler, tid, wants_ai=False)
    row = await _row(maker, tid)
    assert (row.status, row.attempts, ran) == ("running", 1, [])
    async with maker() as db:
        assert await ledger.reap(db) == 1
        await db.commit()
    await runtime.execute("plan_work", handler, tid, wants_ai=False)  # the relay's re-send
    row = await _row(maker, tid)
    assert (row.status, row.attempts, ran) == ("done", 2, [1])


async def test_a_task_that_waited_over_a_minute_is_logged(maker, caplog):
    """#124's PRD 1: a claim more than ledger.SLOW_WAIT after run_after warns with id, kind, queue, wait."""
    async def handler(db, ai, task, payload):
        return {}

    async with maker() as db:
        late = await ledger.add(db, "plan_work", run_after=utcnow() - timedelta(seconds=90))
        prompt = await ledger.add(db, "ingest_tick")
        await db.commit()
    with caplog.at_level("WARNING", logger="jobfinder.tasks"):
        await runtime.execute("plan_work", handler, late.id, wants_ai=False)
        await runtime.execute("ingest_tick", handler, prompt.id, wants_ai=False)
    slow = [r.getMessage() for r in caplog.records if "before claim" in r.getMessage()]
    assert len(slow) == 1 and slow[0].startswith(f"task {late.id} (plan_work) on io waited 9")


async def test_a_delivery_never_raises_into_taskiq(maker, caplog):
    """Postgres unreachable at the claim: logged once, and the message is acked (the relay re-sends)."""

    class Down:
        def __call__(self):
            return self

        async def __aenter__(self):
            raise OSError("connection refused")

        async def __aexit__(self, *exc):
            return False

    async def handler(db, ai, task, payload):
        raise AssertionError("must not run")

    await runtime.execute("plan_work", handler, 1, wants_ai=False, session_factory=Down())
    assert "relay or the reaper will recover it" in caplog.text


async def test_follow_ups_are_published_with_the_result_not_before(maker, published, monkeypatch):
    async def handler(db, ai, task, payload):
        await ledger.add(db, "plan_work", dedupe=True)
        assert await published.settle() == [("io", "ingest_tick", tid)]  # only the delivered row so far
        return {"new": 1}

    tid = await _queued(maker, "ingest_tick")
    await runtime.execute("ingest_tick", handler, tid, wants_ai=False)
    follow = await _follow_up(maker, "plan_work")
    assert await published.settle() == [("io", "ingest_tick", tid), ("io", "plan_work", follow.id)]


async def test_a_failed_handler_publishes_no_follow_up(maker, published):
    async def handler(db, ai, task, payload):
        await ledger.add(db, "plan_work")
        raise RuntimeError("broken")

    tid = await _queued(maker, "ingest_tick")
    await runtime.execute("ingest_tick", handler, tid, wants_ai=False)
    assert await published.settle() == [("io", "ingest_tick", tid)]
    assert await count(maker, Task, kind="plan_work") == 0


async def _follow_up(maker, kind) -> Task:
    async with maker() as db:
        return await db.scalar(sa.select(Task).where(Task.kind == kind))


@pytest.mark.real_commit
async def test_an_ai_task_finishes_while_an_io_task_is_in_flight(maker, monkeypatch):
    """#48's acceptance: a cover letter is still drafted while a full ingest run is in flight. The io
    task here holds its delivery (and its heartbeat) open until the ai one is done."""
    monkeypatch.setattr(get_settings(), "task_heartbeat_seconds", 0.05)
    release = asyncio.Event()

    async def long_ingest(db, ai, task, payload):
        await asyncio.wait_for(release.wait(), 5)
        return {"new": 0}

    async def letter(db, ai, task, payload):
        return {"match_id": payload.match_id}

    ingest = await _queued(maker, "ingest_tick")
    draft = await _queued(maker, "cover_letter", {"match_id": 7})
    io_run = asyncio.create_task(runtime.execute("ingest_tick", long_ingest, ingest, wants_ai=False))
    await asyncio.sleep(0.2)
    assert (await _row(maker, ingest)).status == "running"
    await runtime.execute("cover_letter", letter, draft, payload_model=ai_tasks.CoverLetterArgs, ai=object())
    assert ((await _row(maker, draft)).status, (await _row(maker, ingest)).status) == ("done", "running")
    release.set()
    await io_run
    assert (await _row(maker, ingest)).status == "done"
