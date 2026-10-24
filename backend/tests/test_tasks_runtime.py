"""tasks.runtime (#48): what a delivery does with every outcome, and that the queues are separate
consumers (a long io task never holds up an ai one, nor bulk ai work a priority one)."""
import asyncio
import contextlib
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


def test_each_worker_waits_for_its_tasks_within_its_pods_grace():
    """worker-ai and worker-priority wait 180s on SIGTERM (under their pods' 200s grace), so a pod KEDA
    scales in finishes its matching slice; the others 50s (under 60s)."""
    for queue in brokers.BROKERS:
        argv = worker_argv(queue)
        wait = int(argv[argv.index("--wait-tasks-timeout") + 1])
        assert wait == (180 if queue in ("ai", "priority") else 50), queue


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


@pytest.mark.real_commit
@pytest.mark.parametrize("outcome", ["finish", "fail"])
async def test_a_run_that_ends_after_its_row_was_reaped_keeps_nothing(maker, published, outcome):
    """#136: the reaper took the row back mid-run; the late run's outcome, writes and follow-ups are dropped."""
    async def handler(db, ai, task, payload):
        async with maker() as other:  # the reaper, while this run is still alive
            await other.execute(sa.update(Task).where(Task.id == task.id)
                                .values(locked_at=utcnow() - timedelta(hours=1)))
            assert await ledger.reap(other) == 1
            await other.commit()
        await ledger.add(db, "plan_work")
        if outcome == "fail":
            raise RuntimeError("late")
        return {"late": True}

    tid = await _queued(maker, "ingest_tick")
    await runtime.execute("ingest_tick", handler, tid, wants_ai=False)
    row = await _row(maker, tid)
    assert (row.status, row.attempts, row.result, row.error) == ("queued", 1, None, None)
    assert await count(maker, Task, kind="plan_work") == 0
    assert await published.settle() == [("io", "ingest_tick", tid)]


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


# ---------------------------------------------------------------- deadline (#136)


@contextlib.asynccontextmanager
async def _slot_like():  # like ClaudeGate.slot: an async context manager that waits on entry
    await asyncio.sleep(60)
    yield


async def _stuck_deep():
    async with _slot_like():
        pass


async def hangs_past_its_deadline(db, ai, task, payload):
    await _stuck_deep()
    return {}


async def test_a_run_past_its_deadline_is_cancelled_logged_and_retried(maker, monkeypatch, caplog):
    """#136: a hung handler no longer holds its row `running` behind fresh heartbeats. At the deadline its
    await stack is logged (down to where it hung), it is cancelled, and the row is queued again."""
    monkeypatch.setattr(get_settings(), "task_deadline_minutes", 0.002)  # 0.12s
    tid = await _queued(maker, "ai_check")
    with caplog.at_level("INFO", logger="jobfinder.tasks"):
        await asyncio.wait_for(runtime.execute("ai_check", hangs_past_its_deadline, tid, ai=object()), 5)
    row = await _row(maker, tid)
    assert (row.status, row.attempts) == ("queued", 1) and row.error.startswith("timeout: ")
    passed = [r for r in caplog.records if "passed its" in r.getMessage()]
    assert len(passed) == 1 and passed[0].levelname == "ERROR"
    stack = passed[0].getMessage()
    assert "in hangs_past_its_deadline" in stack and "in _stuck_deep" in stack
    assert stack.index("hangs_past_its_deadline") < stack.index("_stuck_deep")  # outermost first
    assert "in _slot_like" in stack  # through the context manager's generator, down to where it waits


async def test_a_run_past_its_deadline_on_its_last_attempt_fails(maker, monkeypatch):
    monkeypatch.setattr(get_settings(), "task_deadline_minutes", 0.002)
    tid = await _queued(maker, "ai_check")
    async with maker() as db:
        (await db.get(Task, tid)).attempts = get_settings().task_max_attempts - 1
        await db.commit()
    await asyncio.wait_for(runtime.execute("ai_check", hangs_past_its_deadline, tid, ai=object()), 5)
    row = await _row(maker, tid)
    assert (row.status, row.attempts) == ("failed", 3) and row.error.startswith("timeout: ")


async def test_a_handler_that_ignores_its_cancellation_does_not_hold_the_delivery(maker, monkeypatch):
    """The delivery gives a cancelled run CANCEL_WAIT_SECONDS to unwind, then records the timeout anyway;
    the heartbeat cap (below) lets the reaper recover the row if even that record fails."""
    monkeypatch.setattr(get_settings(), "task_deadline_minutes", 0.002)
    monkeypatch.setattr(runtime, "CANCEL_WAIT_SECONDS", 0.1)
    released = asyncio.Event()

    async def stubborn(db, ai, task, payload):
        while not released.is_set():
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                pass  # swallowed
        return {}

    tid = await _queued(maker, "ai_check")
    try:
        await asyncio.wait_for(runtime.execute("ai_check", stubborn, tid, ai=object()), 3)
    finally:
        released.set()
    row = await _row(maker, tid)
    assert row.status == "queued" and row.error.startswith("timeout: ")


async def test_the_heartbeat_stops_at_its_cap(maker, monkeypatch):
    """Beats past the deadline + HEARTBEAT_GRACE would keep a run whose cancellation never landed out of
    the reaper's reach forever (#136)."""
    import time

    monkeypatch.setattr(get_settings(), "task_heartbeat_seconds", 0.01)
    beats = []

    async def heartbeat(db, task_id, *, attempt):
        beats.append(time.monotonic())

    monkeypatch.setattr(ledger, "heartbeat", heartbeat)
    until = time.monotonic() + 0.1
    await asyncio.wait_for(runtime._heartbeat_loop(1, maker, attempt=1, until=until), 2)  # returns: it stopped
    assert beats and max(beats) < until


async def test_a_delivery_cancelled_mid_run_cancels_its_handler(maker):
    """Worker shutdown cancels the delivery; the handler must not run on with a closed session."""
    started, ended = asyncio.Event(), []

    async def handler(db, ai, task, payload):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            ended.append("cancelled")
            raise
        return {}

    tid = await _queued(maker, "ai_check")
    delivery = asyncio.create_task(runtime.execute("ai_check", handler, tid, ai=object()))
    await asyncio.wait_for(started.wait(), 5)
    delivery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await delivery
    assert ended == ["cancelled"]


async def test_each_claimed_run_logs_one_outcome_line(maker, caplog):
    async def done(db, ai, task, payload):
        return {}

    async def rate_limited(db, ai, task, payload):
        raise AIError("rate_limited", "slow down")

    async def broken(db, ai, task, payload):
        raise RuntimeError("broken")

    ids = [await _queued(maker, "ai_check") for _ in range(3)]
    with caplog.at_level("INFO", logger="jobfinder.tasks"):
        for tid, handler in zip(ids, (done, rate_limited, broken), strict=True):
            await runtime.execute("ai_check", handler, tid, ai=object())
        await runtime.execute("ai_check", done, ids[0], ai=object())  # a duplicate: dropped, no outcome
    outcomes = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert len(outcomes) == 3
    assert outcomes[0].startswith(f"task {ids[0]} (ai_check) done in ")
    assert outcomes[1].startswith(f"task {ids[1]} (ai_check) will retry, failed after ")
    assert outcomes[1].endswith("slow down")
    assert outcomes[2].startswith(f"task {ids[2]} (ai_check) failed after ")
    assert outcomes[2].endswith("RuntimeError: broken")


def test_extract_profile_has_its_own_deadline_and_every_other_kind_the_default(monkeypatch):
    """extract_profile's worst case is a 600s gate wait plus two 240s CLI calls: 20 minutes. Every other
    kind reads TASK_DEADLINE_MINUTES at run time."""
    assert ai_tasks and io_tasks and render_tasks
    monkeypatch.setattr(get_settings(), "task_deadline_minutes", 7.5)
    assert runtime.DEADLINES["extract_profile"] == timedelta(minutes=20)
    for kind in set(runtime.REGISTRY) - {"extract_profile"}:
        assert runtime.DEADLINES[kind] is None, kind
        assert runtime.deadline_for(kind) == timedelta(minutes=7.5), kind
    assert runtime.deadline_for("extract_profile") == timedelta(minutes=20)
