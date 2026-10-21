"""tasks.failures: a task that fails for good finishes the item it was meant to finish, so the SPA stops
polling it. Uploads and profile links end as "error" (and no longer hold up the profile extract), pasted
job links as "unresolved"; and an item left processing with no task at all is settled when listed."""
import os
from datetime import timedelta

import pytest

import jobfinder.tasks.ai  # registers the handlers
import jobfinder.tasks.io  # noqa: F401
from jobfinder.core.clock import utcnow
from jobfinder.core.config import get_settings
from jobfinder.integrations.claude.claude_code import AIError
from jobfinder.models import JobLink, Task
from jobfinder.scheduler import maintenance
from jobfinder.services import documents
from jobfinder.services.resolver import GAVE_UP
from jobfinder.tasks import ledger, runtime
from jobfinder.tasks.kinds import QUEUES
from tests.test_documents import _post, _run


async def _deliver(kind, task_id, ai=None):
    await runtime.execute(kind, runtime.REGISTRY[kind], task_id, payload_model=runtime.PAYLOADS[kind],
                          wants_ai=QUEUES[kind] == "ai", ai=ai)


async def _stale(maker, task_id, *, attempts):
    """The task as a worker that died mid-run leaves it, after `attempts` tries."""
    async with maker() as db:
        t = await ledger.claim(db, task_id)
        t.attempts, t.locked_at = attempts, utcnow() - timedelta(hours=1)
        await db.commit()


async def _doc(authed, doc_id) -> dict:
    return (await authed.get(f"/api/documents/{doc_id}")).json()


async def test_a_parse_that_fails_unexpectedly_ends_the_document_as_error(authed, maker, monkeypatch, isolated_store):
    async def broken(*a, **kw):
        raise RuntimeError("store unreachable")

    monkeypatch.setattr(documents, "parse", broken)
    r = (await _post(authed, "a.txt", b"never read")).json()
    task = await _run(maker, r["task_id"])
    assert task.status == "failed"
    [doc] = (await authed.get("/api/documents")).json()
    assert doc["status"] == "error"
    me = (await authed.get("/api/auth/me")).json()["id"]
    assert isolated_store.get(me, doc["id"]).fetch_error == documents.UNFINISHED["parse_document"]
    assert (await authed.post("/api/onboarding/extract")).status_code != 409  # no longer "still reading"


async def test_a_retry_leaves_the_document_processing(authed, maker, monkeypatch):
    async def busy(*a, **kw):
        raise AIError("rate_limited", "later")  # retryable: the task goes back to the queue

    monkeypatch.setattr(documents, "parse", busy)
    r = (await _post(authed, "a.txt", b"read later")).json()
    task = await _run(maker, r["task_id"])
    assert task.status == "queued"
    assert (await _doc(authed, r["document"]["id"]))["status"] == "processing"


async def test_the_reaper_giving_up_on_a_parse_ends_the_document_as_error(authed, maker, isolated_store, monkeypatch):
    """The scheduler mounts no document store, so the reaper leaves the file alone; with no task left for
    it, the next listing settles it (a stuck task's item is older than NO_TASK_GRACE by then)."""
    from jobfinder.context import default_context

    me = (await authed.get("/api/auth/me")).json()["id"]
    r = (await _post(authed, "a.txt", b"worker died")).json()
    await _stale(maker, r["task_id"], attempts=get_settings().task_max_attempts)
    monkeypatch.setattr(default_context(), "store", None)  # as in the scheduler pod
    await maintenance.tick()
    monkeypatch.setattr(default_context(), "store", isolated_store)
    async with maker() as db:
        assert (await db.get(Task, r["task_id"])).status == "failed"
    assert isolated_store.get(me, r["document"]["id"]).status == "processing"
    _age(isolated_store, me, r["document"]["id"], timedelta(minutes=get_settings().task_stuck_after_minutes))
    assert (await _doc(authed, r["document"]["id"]))["status"] == "processing"  # a single GET doesn't settle
    assert [d["status"] for d in (await authed.get("/api/documents")).json()] == ["error"]


async def test_the_reaper_requeueing_a_fetch_leaves_the_link_processing(authed, maker, isolated_store):
    me = (await authed.get("/api/auth/me")).json()["id"]
    r = (await authed.post("/api/links", json={"kind": "other", "url": "https://example.com/me"})).json()
    _age(isolated_store, me, r["link"]["id"], timedelta(hours=1))
    await _stale(maker, r["task_id"], attempts=1)
    await maintenance.tick()
    [link] = (await authed.get("/api/links")).json()
    assert link["status"] == "processing"  # requeued: the task is still there for it
    await _stale(maker, r["task_id"], attempts=get_settings().task_max_attempts)
    await maintenance.tick()
    [link] = (await authed.get("/api/links")).json()
    assert link["status"] == "error" and link["fetch_error"] == documents.UNFINISHED["fetch_link"]


async def test_a_document_marked_unfinished_is_read_when_its_task_is_retried(authed, maker, monkeypatch):
    """An admin's retry (ledger.requeue) of the failed parse reads the file after all."""
    real = documents.parse

    async def broken(*a, **kw):
        raise RuntimeError("store unreachable")

    monkeypatch.setattr(documents, "parse", broken)
    r = (await _post(authed, "a.txt", b"read on retry")).json()
    await _run(maker, r["task_id"])
    assert (await _doc(authed, r["document"]["id"]))["status"] == "error"
    monkeypatch.setattr(documents, "parse", real)
    async with maker() as db:
        await ledger.requeue(db, r["task_id"])
        await db.commit()
    assert (await _run(maker, r["task_id"])).status == "done"
    doc = await _doc(authed, r["document"]["id"])
    assert doc["status"] == "ready" and doc["chars"] == len("read on retry")


async def test_settle_drops_a_document_deleted_while_listing(authed, isolated_store, monkeypatch):
    me = (await authed.get("/api/auth/me")).json()["id"]
    meta, _ = isolated_store.put(me, "resume", "a.txt", text="", data=b"gone", ext=".txt", status="processing")
    _age(isolated_store, me, meta.id, timedelta(hours=1))
    real = documents.unfinished

    def deleted_first(store, user_id, doc_id):
        store.delete(user_id, doc_id)
        return real(store, user_id, doc_id)

    monkeypatch.setattr(documents, "unfinished", deleted_first)
    assert (await authed.get("/api/documents")).json() == []


def _age(store, user_id, doc_id, by: timedelta):
    path = store._doc_dir(user_id, doc_id) / "meta.json"
    then = (utcnow() - by).timestamp()
    os.utime(path, (then, then))


async def test_a_document_left_processing_with_no_task_is_settled_when_listed(authed, maker, isolated_store):
    """The upload's request stored the file, then its commit (and so its parse_document) was lost."""
    me = (await authed.get("/api/auth/me")).json()["id"]
    meta, _ = isolated_store.put(me, "resume", "a.txt", text="", data=b"orphan", ext=".txt", status="processing")
    assert [d["status"] for d in (await authed.get("/api/documents")).json()] == ["processing"]  # within the grace
    assert (await authed.post("/api/onboarding/extract")).status_code == 409
    _age(isolated_store, me, meta.id, documents.NO_TASK_GRACE + timedelta(seconds=5))
    assert [d["status"] for d in (await authed.get("/api/documents")).json()] == ["error"]
    assert (await authed.post("/api/onboarding/extract")).status_code != 409


async def test_a_document_whose_task_is_queued_is_not_settled(authed, isolated_store):
    r = (await _post(authed, "a.txt", b"waiting in a long queue")).json()
    me = (await authed.get("/api/auth/me")).json()["id"]
    _age(isolated_store, me, r["document"]["id"], timedelta(hours=1))
    assert [d["status"] for d in (await authed.get("/api/documents")).json()] == ["processing"]


@pytest.fixture
async def resolving(authed, maker):
    me = (await authed.get("/api/auth/me")).json()["id"]
    async with maker() as db:
        link = JobLink(user_id=me, url="https://careers.acme.com/jobs/1", status="resolving")
        db.add(link)
        await db.flush()
        task = await ledger.add(db, "induce_recipe", {"link_id": link.id}, user_id=me)
        await db.commit()
    return link.id, task.id


async def test_an_induce_recipe_that_fails_unexpectedly_leaves_the_link_unresolved(maker, monkeypatch, resolving):
    async def broken(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr("jobfinder.services.resolver.llm_allowed", broken)
    link_id, task_id = resolving
    await _deliver("induce_recipe", task_id)
    async with maker() as db:
        assert (await db.get(Task, task_id)).status == "failed"
        link = await db.get(JobLink, link_id)
    assert (link.status, link.error) == ("unresolved", GAVE_UP)


async def test_the_reaper_giving_up_on_induce_recipe_leaves_the_link_unresolved(maker, resolving):
    link_id, task_id = resolving
    await _stale(maker, task_id, attempts=get_settings().task_max_attempts)
    await maintenance.tick()
    async with maker() as db:
        link = await db.get(JobLink, link_id)
    assert (link.status, link.error) == ("unresolved", GAVE_UP)


async def test_fail_says_when_it_is_the_end(db):
    t = await ledger.add(db, "plan_work", rerun=True)
    await ledger.claim(db, t.id)
    assert await ledger.fail(db, t, "busy", retry=True) is False  # retried
    t2 = await ledger.add(db, "fetch_link", {"doc_id": "x"}, user_id=None, rerun=True)
    await ledger.claim(db, t2.id)
    await ledger.add(db, "fetch_link", {"doc_id": "x"}, user_id=None, rerun=True)  # a rerun waits behind it
    assert await ledger.fail(db, t2, "bad", retry=False) is False  # the rerun does the work
    t3 = await ledger.add(db, "fetch_link", {"doc_id": "y"}, user_id=None)
    await ledger.claim(db, t3.id)
    assert await ledger.fail(db, t3, "bad", retry=False) is True
