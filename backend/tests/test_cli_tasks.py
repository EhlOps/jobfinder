import pytest

from jobfinder.models import Task
from jobfinder.tasks import ledger
from tests.cli_runner import invoke

pytestmark = pytest.mark.real_commit


async def _failed_task(maker, kind="ingest_jobs") -> int:
    async with maker() as db:
        task = await ledger.add(db, kind)
        await ledger.fail(db, task, "boom", retry=False)
        await db.commit()
        return task.id


async def test_list_filters_and_shows_the_columns(maker):
    failed = await _failed_task(maker)
    async with maker() as db:
        await ledger.add(db, "daily_emails")
        await db.commit()
    r = await invoke("tasks", "list")
    assert r.exit_code == 0
    header = r.stdout.splitlines()[0].split()
    assert header == ["id", "kind", "queue", "status", "att", "user", "created", "finished"]
    assert "ingest_jobs" in r.stdout and "daily_emails" in r.stdout and "2 tasks" in r.stdout
    r = await invoke("tasks", "list", "--status", "failed")
    assert f"{failed:<8} ingest_jobs" in r.stdout and "daily_emails" not in r.stdout
    assert "1 tasks" in (await invoke("tasks", "list", "--kind", "daily_emails", "--limit", "5")).stdout
    assert (await invoke("tasks", "list", "--status", "nope")).exit_code == 2


async def test_retry_requeues_and_publishes(maker, published):
    task_id = await _failed_task(maker)
    published.clear()  # the add was published when it committed
    r = await invoke("tasks", "retry", str(task_id))
    assert r.exit_code == 0 and f"Requeued task {task_id} (ingest_jobs)" in r.stdout
    async with maker() as db:
        task = await db.get(Task, task_id)
    assert (task.status, task.attempts, task.error) == ("queued", 0, None)
    assert published == [(task.queue, "ingest_jobs", task_id)]  # sent before the command's loop closed

    r = await invoke("tasks", "retry", str(task_id))  # still queued: Conflict
    assert r.exit_code == 1 and "already queued" in r.stderr
    r = await invoke("tasks", "retry", "999999")
    assert r.exit_code == 1 and "No task 999999" in r.stderr
    assert (await invoke("tasks", "retry", "abc")).exit_code == 2
